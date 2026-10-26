"""Operations journal and staff review for the synthetic clinic."""

import asyncio
import hmac
import json
import logging
import sqlite3
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as day_time
from math import ceil
from pathlib import Path
from time import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from switchboard.config import VISIT_MINUTES
from switchboard.frontdesk import FrontDesk
from switchboard.scheduling import RECEIPT_SYSTEM, Change, instant
from switchboard.store import Conflict, Resource, Unavailable

router = APIRouter()
LOG = logging.getLogger(__name__)
RETENTION_SECONDS = 30 * 24 * 60 * 60
TRANSCRIPT_SECONDS = 7 * 24 * 60 * 60
RECONCILIATION_GRACE_SECONDS = 15
RECONCILIATION_BATCH = 50


@dataclass
class Trace:
    id: str
    transport: str
    config_version: str
    operations: "Operations"
    started: float = field(default_factory=time)
    outcome: str | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=128))
    work: tuple[str, dict] | None = None
    transcript: deque = field(default_factory=lambda: deque(maxlen=400), repr=False)

    def record(self, speaker: str, text: str) -> None:
        if self.operations.capture_transcripts and text.strip():
            self.transcript.append({"speaker": speaker, "text": text[:400]})

    def add(self, event: str, *, state: str | None = None, latency_ms: int | None = None) -> None:
        self.events.append(
            {
                "event": event,
                "state": state,
                "latency_ms": latency_ms,
                "elapsed_ms": round((time() - self.started) * 1000),
            }
        )

    async def begin_mutation(self, operation: str, action: str) -> None:
        await asyncio.to_thread(self.operations._begin_mutation, self.id, operation, action)
        self.add("mutation_dispatched", state=action)

    async def finish_mutation(self, operation: str, status: str) -> None:
        self.add("mutation_" + status)
        await asyncio.to_thread(self.operations._finish_mutation, operation, status)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "transport": self.transport,
            "config_version": self.config_version,
            "started": self.started,
            "outcome": self.outcome,
            "events": list(self.events),
        }


class Operations:
    def __init__(self, path: Path, config_version: str, *, capture_transcripts: bool = False):
        self.path = path
        self.config_version = config_version
        self.capture_transcripts = capture_transcripts
        self.live: dict[str, Trace] = {}
        self.frontdesk: FrontDesk | None = None
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS call_traces (
                    id TEXT PRIMARY KEY, started REAL NOT NULL, body TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS mutations (
                    operation TEXT PRIMARY KEY, trace_id TEXT NOT NULL, action TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, checked REAL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    trace_id TEXT PRIMARY KEY, reviewed_at REAL NOT NULL
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(mutations)")}
            if "checked" not in columns:
                db.execute("ALTER TABLE mutations ADD COLUMN checked REAL")
            db.execute("""
                CREATE INDEX IF NOT EXISTS mutations_reconcile
                ON mutations(COALESCE(checked, created), created, operation)
                WHERE status IN ('pending', 'unknown')
            """)
            db.execute("CREATE INDEX IF NOT EXISTS trace_started ON call_traces(started)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS call_transcripts (
                    id TEXT PRIMARY KEY, started REAL NOT NULL, body TEXT NOT NULL
                )
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=1)

    def start(self, transport: str) -> Trace:
        trace = Trace(uuid4().hex, transport, self.config_version, self)
        trace.add("connected")
        self.live[trace.id] = trace
        return trace

    async def finish(self, trace: Trace) -> None:
        self.live.pop(trace.id, None)
        trace.outcome = trace.outcome or "disconnected"
        trace.add("disconnected")
        await asyncio.to_thread(self._write, trace)

    def _write(self, trace: Trace) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO call_traces VALUES (?, ?, ?)",
                (trace.id, trace.started, json.dumps(trace.snapshot())),
            )
            if trace.transcript:
                db.execute(
                    "INSERT INTO call_transcripts VALUES (?, ?, ?)",
                    (trace.id, trace.started, json.dumps(list(trace.transcript))),
                )
            if self.frontdesk and trace.work:
                reason, context = trace.work
                db.execute(
                    "INSERT OR IGNORE INTO staff_work (id, trace_id, reason, context, updated) VALUES (?, ?, ?, ?, ?)",
                    (trace.id, trace.id, reason, json.dumps(context), time()),
                )

    def _begin_mutation(self, trace_id: str, operation: str, action: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO mutations (operation, trace_id, action, status, created)
                   VALUES (?, ?, ?, 'pending', ?)""",
                (operation, trace_id, action, time()),
            )

    def _finish_mutation(self, operation: str, status: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE mutations SET status=? WHERE operation=? AND status!='committed'",
                (status, operation),
            )

    def _unresolved(self, *, grace: float = 0) -> list[dict]:
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    """SELECT operation, trace_id, action, status, created, checked FROM mutations
                       WHERE status IN ('pending', 'unknown') AND created < ?
                       ORDER BY created DESC LIMIT 50""",
                    (time() - grace,),
                )
            ]

    def _reconciliation_batch(self) -> list[str]:
        with closing(self.connect()) as db:
            return [
                row[0]
                for row in db.execute(
                    """SELECT operation FROM mutations
                       WHERE status IN ('pending', 'unknown') AND created < ?
                       ORDER BY COALESCE(checked, created), created, operation LIMIT ?""",
                    (time() - RECONCILIATION_GRACE_SECONDS, RECONCILIATION_BATCH),
                )
            ]

    def _mark_checked(self, operation: str) -> bool:
        with closing(self.connect()) as db, db:
            return (
                db.execute(
                    "UPDATE mutations SET checked=? WHERE operation=? AND status IN ('pending', 'unknown')",
                    (time(), operation),
                ).rowcount
                == 1
            )

    async def reconcile(self, receipt: Callable[[str], Awaitable[bool]]) -> None:
        for operation in await asyncio.to_thread(self._reconciliation_batch):
            if await asyncio.to_thread(self._mark_checked, operation) and await receipt(operation):
                await asyncio.to_thread(self._finish_mutation, operation, "committed")

    def _settled_mutations(self) -> list[dict]:
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    """SELECT operation, trace_id, action, status, created, checked FROM mutations
                       WHERE status IN ('committed', 'rejected') AND created >= ?
                       ORDER BY COALESCE(checked, created) DESC, operation LIMIT 10""",
                    (time() - RETENTION_SECONDS,),
                )
            ]

    async def purge(self) -> None:
        await asyncio.to_thread(self._purge)

    def _purge(self) -> None:
        with closing(self.connect()) as db, db:
            cutoff = time() - RETENTION_SECONDS
            db.execute("DELETE FROM call_traces WHERE started < ?", (cutoff,))
            db.execute("DELETE FROM mutations WHERE created < ?", (cutoff,))
            db.execute("DELETE FROM reviews WHERE reviewed_at < ?", (cutoff,))
            db.execute(
                "DELETE FROM call_transcripts WHERE started < ?", (time() - TRANSCRIPT_SECONDS,)
            )

    def call_detail(self, trace_id: str) -> dict | None:
        live = self.live.get(trace_id)
        with closing(self.connect()) as db:
            stored = db.execute(
                """SELECT body, reviewed_at FROM call_traces LEFT JOIN reviews
                   ON reviews.trace_id=call_traces.id WHERE id=? AND started>=?""",
                (trace_id, time() - RETENTION_SECONDS),
            ).fetchone()
            if not live and not stored:
                return None
            transcript = db.execute(
                "SELECT body FROM call_transcripts WHERE id=? AND started>=?",
                (trace_id, time() - TRANSCRIPT_SECONDS),
            ).fetchone()
            return {
                "call": live.snapshot()
                if live
                else {**json.loads(stored[0]), "reviewed_at": stored[1]},
                "transcript": json.loads(transcript[0]) if transcript else [],
            }

    def appointment_links(self, operation_ids: set[str]) -> dict:
        if not operation_ids:
            return {}
        ids = sorted(operation_ids)
        placeholders = ",".join("?" for _ in ids)
        with closing(self.connect()) as db:
            return {
                operation: {"call_id": trace_id, "confirmation": status}
                for operation, trace_id, status in db.execute(
                    f"""SELECT m.operation, t.id, n.status FROM mutations m
                         LEFT JOIN call_traces t ON t.id=m.trace_id AND t.started>=?
                         LEFT JOIN appointment_notices n ON n.operation=m.operation
                         WHERE m.operation IN ({placeholders})""",
                    [time() - RETENTION_SECONDS, *ids],
                )
            }

    def _recent(self) -> list[dict]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT body, reviewed_at FROM call_traces
                   LEFT JOIN reviews ON reviews.trace_id = call_traces.id
                   WHERE started >= ? ORDER BY started DESC LIMIT 50""",
                (time() - RETENTION_SECONDS,),
            )
            return [{**json.loads(body), "reviewed_at": reviewed} for body, reviewed in rows]

    async def mark_reviewed(self, trace_id: str) -> bool:
        return await asyncio.to_thread(self._mark_reviewed, trace_id)

    def _mark_reviewed(self, trace_id: str) -> bool:
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT body FROM call_traces WHERE id=? AND started >= ?",
                (trace_id, time() - RETENTION_SECONDS),
            ).fetchone()
            if not row or json.loads(row[0])["outcome"] not in {
                "emergency",
                "handoff",
                "needs_review",
            }:
                return False
            db.execute("INSERT OR IGNORE INTO reviews VALUES (?, ?)", (trace_id, time()))
            return True

    async def snapshot(self) -> dict:
        recent = await asyncio.to_thread(self._recent)
        durations = sorted(
            entry["latency_ms"]
            for call in recent
            for entry in call["events"]
            if entry["event"] == "turn_processed" and entry["latency_ms"] is not None
        )

        def percentile(fraction: float) -> int | None:
            return durations[ceil(len(durations) * fraction) - 1] if durations else None

        return {
            "live": [call.snapshot() for call in self.live.values()],
            "recent": recent,
            "unresolved_mutations": await asyncio.to_thread(
                self._unresolved, grace=RECONCILIATION_GRACE_SECONDS
            ),
            "settled_mutations": await asyncio.to_thread(self._settled_mutations),
            "outcomes": Counter(item["outcome"] for item in recent),
            "turns": {"count": len(durations), "p50": percentile(0.5), "p95": percentile(0.95)},
        }


@router.get("/operations")
async def operations(request: Request):
    if not request.app.state.ops_token:
        raise HTTPException(404)
    return FileResponse(
        Path(__file__).parent / "static" / "operations.html",
        headers={"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"},
    )


def authorize(request: Request, authorization: str) -> None:
    token = request.app.state.ops_token
    if not token:
        raise HTTPException(404)
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})


@router.get("/api/operations")
async def summary(request: Request, authorization: str = Header(default="")):
    authorize(request, authorization)
    operations = request.app.state.ops
    data = await operations.snapshot()
    if operations.frontdesk:
        data.update(await operations.frontdesk.snapshot())
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


class TaskAction(BaseModel):
    action: str
    revision: int = Field(ge=0)
    value: str = Field(min_length=1, max_length=60)


@router.get("/api/operations/work/{task_id}/context")
async def inspect_work(task_id: str, request: Request, authorization: str = Header(default="")):
    authorize(request, authorization)
    context = await request.app.state.frontdesk.work_context(task_id)
    if context is None:
        raise HTTPException(404)
    scheduling = request.app.state.scheduling
    clinic = scheduling.clinic
    result = {}
    try:
        if reference := context.get("patient_reference"):
            patient = reference.removeprefix("Patient/")
            appointments = await scheduling.appointments(patient)
            result["Appointments on file"] = [
                f"{instant(item['start']).astimezone(clinic.timezone):%A, %b %d at %I:%M %p}"
                f" with {clinic.providers.get(scheduling.provider_for(item), 'the clinic')}"
                for item in appointments
            ] or ["No upcoming appointments"]
            if service := context.get("service"):
                approved = await scheduling.has_referral(patient, service)
                result["Active referral"] = "On file" if approved else "Needs verification"
        if slot_id := context.get("slot"):
            slot = await scheduling.store.read("Slot", slot_id)
            available = (
                slot and slot["status"] == "free" and instant(slot["start"]) > scheduling.now()
            )
            result["Slot now"] = "Available" if available else "Unavailable"
        if operation := context.get("operation"):
            result["Operation receipt"] = (
                "Commit verified"
                if await scheduling.receipt(operation)
                else "Commit has not been verified"
            )
    except Unavailable as exc:
        raise HTTPException(503, "The scheduling record is temporarily unavailable") from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/api/operations/work/{task_id}", status_code=204)
async def update_work(
    task_id: str, action: TaskAction, request: Request, authorization: str = Header(default="")
):
    authorize(request, authorization)
    value = action.value.strip()
    outcomes = {
        "Patient contacted",
        "Referred to scheduling",
        "Referral verified",
        "Write reconciled",
        "Closed without booking",
    }
    if (
        not value
        or (action.action == "resolve" and value not in outcomes)
        or action.action not in {"claim", "resolve"}
    ):
        raise HTTPException(422, "Select a valid action and resolution")
    frontdesk = request.app.state.ops.frontdesk
    if not await frontdesk.update_task(task_id, action.revision, action.action, value, value):
        raise HTTPException(409, "This work item has changed. Refresh and try again.")
    return Response(status_code=204)


@router.post("/api/operations/{trace_id}/review", status_code=204)
async def mark_reviewed(trace_id: str, request: Request, authorization: str = Header(default="")):
    authorize(request, authorization)
    if not await request.app.state.ops.mark_reviewed(trace_id):
        raise HTTPException(404)
    return Response(status_code=204)


@router.get("/api/operations/calls/{trace_id}")
async def inspect_call(trace_id: str, request: Request, authorization: str = Header(default="")):
    authorize(request, authorization)
    result = await asyncio.to_thread(request.app.state.ops.call_detail, trace_id)
    if result is None:
        raise HTTPException(404, "Call details have expired or are unavailable")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def patient_id(appointment: Resource) -> str | None:
    for entry in appointment.get("participant", []):
        reference = entry.get("actor", {}).get("reference", "")
        if reference.startswith("Patient/"):
            return reference.removeprefix("Patient/")
    return None


def visit_type(appointment: Resource) -> str | None:
    duration = instant(appointment["end"]) - instant(appointment["start"])
    return next(
        (name for name, minutes in VISIT_MINUTES.items() if duration == timedelta(minutes=minutes)),
        None,
    )


@router.get("/api/operations/appointments")
async def staff_calendar(
    request: Request,
    day: date | None = None,
    provider: str | None = None,
    authorization: str = Header(default=""),
):
    authorize(request, authorization)
    scheduling = request.app.state.scheduling
    clinic = scheduling.clinic
    today = scheduling.now().astimezone(clinic.timezone).date()
    day = day or today
    if abs((day - today).days) > 366 or (provider and provider not in clinic.providers):
        raise HTTPException(422, "Choose a valid date and provider")
    start = datetime.combine(day, day_time.min, clinic.timezone)
    end = datetime.combine(day + timedelta(days=1), day_time.min, clinic.timezone)
    try:
        records = await scheduling.store.search("Appointment", date=f"ge{start.isoformat()}")
        records = sorted(
            (
                item
                for item in records
                if item.get("start")
                and item.get("end")
                and start <= instant(item["start"]) < end
                and scheduling.provider_for(item) in clinic.providers
                and (not provider or scheduling.provider_for(item) == provider)
            ),
            key=lambda item: (instant(item["start"]), item["id"]),
        )
        operations = {
            item["id"]: next(
                (
                    i.get("value")
                    for i in item.get("identifier", [])
                    if i.get("system") == RECEIPT_SYSTEM
                ),
                None,
            )
            for item in records
        }
        links = await asyncio.to_thread(
            request.app.state.ops.appointment_links, {op for op in operations.values() if op}
        )
        names = {}
        for patient in {patient_id(item) for item in records} - {None}:
            record = await scheduling.store.read("Patient", patient)
            name = next(iter((record or {}).get("name", [])), {})
            names[patient] = (
                name.get("text")
                or " ".join([*name.get("given", []), name.get("family", "")]).strip()
            )
    except (Unavailable, sqlite3.Error) as exc:
        raise HTTPException(503, "The appointment calendar is temporarily unavailable") from exc
    return JSONResponse(
        {
            "day": day.isoformat(),
            "timezone": clinic.timezone.key,
            "providers": clinic.providers,
            "appointments": [
                {
                    "id": item["id"],
                    "revision": item.get("meta", {}).get("versionId"),
                    "patient": names.get(patient_id(item)) or "Patient name unavailable",
                    "provider": clinic.providers[scheduling.provider_for(item)],
                    "location": clinic.locations.get(scheduling.location_for(item)),
                    "start": item["start"],
                    "end": item["end"],
                    "status": item["status"],
                    "visit": visit_type(item),
                    "can_change": item["status"] == "booked"
                    and scheduling.can_change(item)
                    and bool(
                        patient_id(item)
                        and item.get("slot")
                        and item.get("meta", {}).get("versionId")
                    ),
                    **links.get(operations[item["id"]], {}),
                }
                for item in records
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


class AppointmentAction(BaseModel):
    action: Literal["cancel", "reschedule"]
    revision: str = Field(min_length=1, max_length=64)
    slot: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.-]{1,64}$")


async def changeable_appointment(request: Request, appointment_id: str) -> Resource:
    scheduler = request.app.state.scheduling
    if (
        not appointment_id
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
            for c in appointment_id
        )
        or len(appointment_id) > 64
    ):
        raise HTTPException(404)
    item = await scheduler.store.read("Appointment", appointment_id)
    if not item or scheduler.provider_for(item) not in scheduler.clinic.providers:
        raise HTTPException(404)
    if item.get("status") != "booked" or not scheduler.can_change(item) or not patient_id(item):
        raise HTTPException(409, "This appointment is no longer eligible for changes")
    return item


@router.get("/api/operations/appointments/{appointment_id}/openings")
async def staff_openings(
    appointment_id: str, day: date, request: Request, authorization: str = Header(default="")
):
    authorize(request, authorization)
    scheduler = request.app.state.scheduling
    try:
        item = await changeable_appointment(request, appointment_id)
        visit = visit_type(item)
        if not visit:
            raise HTTPException(409, "The appointment duration is unsupported")
        options = await scheduler.options(visit, day, provider=scheduler.provider_for(item))
    except Unavailable as exc:
        raise HTTPException(503, "Openings are temporarily unavailable") from exc
    return JSONResponse(
        [{"id": slot["id"], "start": slot["start"], "end": slot["end"]} for slot in options],
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/operations/appointments/{appointment_id}", status_code=204)
async def staff_change(
    appointment_id: str,
    action: AppointmentAction,
    request: Request,
    authorization: str = Header(default=""),
):
    authorize(request, authorization)
    state = request.app.state
    scheduler = state.scheduling
    clinic = scheduler.clinic
    if (action.action == "reschedule") != bool(action.slot):
        raise HTTPException(422, "Choose a new opening to reschedule")
    hold = trace = change = None
    journaled = False
    status = "unknown"
    try:
        item = await changeable_appointment(request, appointment_id)
        if item.get("meta", {}).get("versionId") != action.revision:
            raise Conflict("Stale appointment")
        patient = patient_id(item)
        assert patient
        slot = None
        if action.slot:
            slot = await scheduler.store.read("Slot", action.slot)
            if (
                not slot
                or scheduler.provider_for(slot) != scheduler.provider_for(item)
                or instant(slot["end"]) - instant(slot["start"])
                != instant(item["end"]) - instant(item["start"])
                or instant(slot["start"]) > scheduler.now() + timedelta(days=61)
            ):
                raise Conflict("Slot is incompatible")
            hold = await scheduler.hold(slot)
        trace = state.ops.start("staff_console")
        change = Change(hold.operation if hold else uuid4().hex, action.action, patient, item, hold)
        details = ""
        if slot:
            provider = clinic.providers[scheduler.provider_for(slot)]
            location = clinic.locations.get(scheduler.location_for(slot), clinic.name)
            local = instant(slot["start"]).astimezone(clinic.timezone)
            details = f"{local:%A, %B %d, %Y at %I:%M %p %Z} with {provider} at {location}."
        await state.frontdesk.stage(
            change,
            trace.id,
            clinic.name,
            details,
            await scheduler.email_on_file(patient) if hold else None,
        )
        await trace.begin_mutation(change.operation, change.action)
        journaled = True
        await scheduler.commit(change)
        status = "committed"
        trace.outcome = "rescheduled" if hold else "cancelled"
        if hold:
            try:
                await state.frontdesk.activate_notice(change.operation)
            except sqlite3.Error:
                LOG.warning("Staff reschedule confirmation will be reconciled")
    except Conflict as exc:
        status = "rejected"
        raise HTTPException(
            409, "The appointment or opening changed. Refresh and try again."
        ) from exc
    except (Unavailable, sqlite3.Error) as exc:
        raise HTTPException(
            503, "Refresh the calendar to check the outcome before retrying."
        ) from exc
    finally:
        if hold and status != "committed":
            try:
                await scheduler.release(hold)
            except Unavailable:
                pass
        if trace:
            try:
                if journaled:
                    await trace.finish_mutation(change.operation, status)
            except sqlite3.Error:
                LOG.warning("Staff appointment journal update unavailable")
            try:
                await state.ops.finish(trace)
            except sqlite3.Error:
                LOG.warning("Staff appointment activity unavailable")
    return Response(status_code=204)
