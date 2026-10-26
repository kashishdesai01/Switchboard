"""Durable staff work, appointment notices, and simulated waitlist offers."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from time import time
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from switchboard.operations import Operations
    from switchboard.scheduling import Change, Scheduling

EMAIL = re.compile(
    r"[A-Za-z0-9_+%'-]+(?:\.[A-Za-z0-9_+%'-]+)*@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}"
)
NOTICE_SECONDS = 24 * 3600
WORK_SECONDS = 30 * 86400
TASK_REASONS = {
    "referral_required": "Verify the referral in the scheduling record, then contact the patient to offer an eligible appointment.",
    "staff_requested": "Review the request and route it to the appropriate desk or callback queue.",
    "identity_unverified": "Follow the clinic identity verification process before discussing appointment details.",
    "appointment_write_uncertain": "Check the operation receipt and appointment record before attempting another write.",
    "no_availability": "Check alternate dates, providers, or locations with the caller.",
    "waitlist_offer": "Recheck the released slot, then contact the patient to offer it. Obtain consent before booking.",
    "emergency": "Review the routing event under the clinic's emergency protocol.",
    "policy_review": "Review the appointment and clinic policy, then follow up with the patient.",
    "email_delivery": "Review the notice delivery status before resending or contacting the patient.",
    "email_address_needed": "Contact the patient through the clinic's verified contact workflow to obtain an email address for the confirmation.",
}


def email_address(value: str) -> str | None:
    address = value.strip()
    if len(address) > 254 or not EMAIL.fullmatch(address) or ".." in address:
        return None
    local, domain = address.rsplit("@", 1)
    return f"{local}@{domain.lower()}" if len(local) <= 64 else None


@dataclass(frozen=True)
class MailSettings:
    host: str | None = None
    port: int = 587
    sender: str = "appointments@switchboard.example"
    username: str | None = None
    password: str | None = None
    allowed: frozenset[str] = frozenset()

    @classmethod
    def configured(cls) -> "MailSettings":
        host = os.getenv("SWITCHBOARD_SMTP_HOST")
        if not host:
            return cls()
        sender = email_address(os.environ["SWITCHBOARD_SMTP_FROM"])
        allowed = frozenset(
            item.strip().casefold()
            for item in os.environ["SWITCHBOARD_SMTP_RECIPIENTS"].split(",")
            if item.strip()
        )
        if not sender or not allowed or any(email_address(item) is None for item in allowed):
            raise ValueError("Configure valid SMTP sender and permitted test recipients")
        return cls(
            host,
            int(os.getenv("SWITCHBOARD_SMTP_PORT", "587")),
            sender,
            os.getenv("SWITCHBOARD_SMTP_USERNAME"),
            os.getenv("SWITCHBOARD_SMTP_PASSWORD"),
            allowed,
        )

    def send(self, notice: dict) -> None:
        assert self.host
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = notice["recipient"]
        message["Subject"] = notice["subject"]
        message["Message-ID"] = notice["message_id"]
        message.set_content(notice["body"])
        with smtplib.SMTP(self.host, self.port, timeout=8) as client:
            client.starttls(context=ssl.create_default_context())
            if self.username:
                client.login(self.username, self.password or "")
            client.send_message(message)


class FrontDesk:
    def __init__(self, operations: "Operations", mail: MailSettings):
        self.connect: Callable[[], sqlite3.Connection] = operations.connect
        self.mail = mail
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS staff_work (
                    id TEXT PRIMARY KEY, trace_id TEXT, reason TEXT NOT NULL,
                    context TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    assignee TEXT, resolution TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS appointment_notices (
                    operation TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    created REAL NOT NULL, trace_id TEXT, subject TEXT NOT NULL,
                    body TEXT NOT NULL, recipient TEXT, message_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available'
                );
                CREATE TABLE IF NOT EXISTS waitlist (
                    id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                    patient TEXT NOT NULL, criteria TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'waiting'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS waitlist_waiting
                    ON waitlist (patient, criteria) WHERE status='waiting';
                CREATE TABLE IF NOT EXISTS released_slots (
                    operation TEXT PRIMARY KEY, slot TEXT NOT NULL, patient TEXT NOT NULL,
                    created REAL NOT NULL, checked REAL, processed INTEGER NOT NULL DEFAULT 0
                );
            """)
            # A process may have stopped after the SMTP server accepted a message.
            db.execute(
                "UPDATE appointment_notices SET status='delivery_unknown' WHERE status='sending'"
            )
            for operation, trace_id in db.execute(
                "SELECT operation, trace_id FROM appointment_notices WHERE status='delivery_unknown'"
            ):
                self._delivery_work(db, operation, trace_id, "delivery_unknown")

    async def snapshot(self) -> dict:
        return await asyncio.to_thread(self._snapshot)

    async def work_context(self, task_id: str) -> dict | None:
        return await asyncio.to_thread(self._work_context, task_id)

    def _work_context(self, task_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT context FROM staff_work WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _snapshot(self) -> dict:
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            tasks = []
            for row in db.execute(
                "SELECT * FROM staff_work ORDER BY status='resolved', updated DESC LIMIT 100"
            ):
                item = dict(row)
                item["context"] = json.loads(item["context"])
                item["next_action"] = TASK_REASONS[item["reason"]]
                tasks.append(item)
            notices = [
                dict(row)
                for row in db.execute("""
                    SELECT operation, trace_id, created, subject, body, status
                    FROM appointment_notices WHERE status NOT IN ('available', 'awaiting_commit')
                    ORDER BY created DESC LIMIT 50
                """)
            ]
            waiting = [
                dict(row)
                for row in db.execute("""
                    SELECT id, trace_id, criteria, created, status FROM waitlist
                    ORDER BY created DESC LIMIT 50
                """)
            ]
            for item in waiting:
                item["criteria"] = json.loads(item["criteria"])
            return {"tasks": tasks, "notices": notices, "waitlist": waiting}

    async def update_task(
        self, task_id: str, revision: int, action: str, assignee: str, resolution: str
    ) -> bool:
        return await asyncio.to_thread(
            self._update_task, task_id, revision, action, assignee, resolution
        )

    def _update_task(
        self, task_id: str, revision: int, action: str, assignee: str, resolution: str
    ) -> bool:
        with closing(self.connect()) as db, db:
            if action == "claim":
                result = db.execute(
                    """
                    UPDATE staff_work SET status='in_progress', assignee=?, revision=revision+1, updated=?
                    WHERE id=? AND revision=? AND status='open'
                """,
                    (assignee, time(), task_id, revision),
                )
            else:
                result = db.execute(
                    """
                    UPDATE staff_work SET status='resolved', resolution=?, revision=revision+1, updated=?
                    WHERE id=? AND revision=? AND status='in_progress'
                """,
                    (resolution, time(), task_id, revision),
                )
            return result.rowcount == 1

    async def stage(
        self,
        change: "Change",
        trace_id: str | None,
        clinic: str,
        details: str,
        recipient: str | None,
    ) -> None:
        await asyncio.to_thread(self._stage, change, trace_id, clinic, details, recipient)

    def _stage(
        self,
        change: "Change",
        trace_id: str | None,
        clinic: str,
        details: str,
        recipient: str | None,
    ) -> None:
        with closing(self.connect()) as db, db:
            if change.hold:
                subject = (
                    "Your appointment confirmation"
                    if change.action == "book"
                    else "Your updated appointment"
                )
                body = f"{clinic}\n\n{subject}\n{details}\n\nPlease contact the clinic if you need to make a change."
                db.execute(
                    """
                    INSERT INTO appointment_notices
                    (operation, token_hash, created, trace_id, subject, body, message_id, recipient, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_commit')
                """,
                    (
                        change.operation,
                        self._hash(secrets.token_urlsafe(32)),
                        time(),
                        trace_id,
                        subject,
                        body,
                        make_msgid(domain="switchboard.example"),
                        recipient,
                    ),
                )
            if change.old and len(change.old.get("slot", [])) == 1:
                reference = change.old["slot"][0].get("reference", "")
                if reference.startswith("Slot/"):
                    db.execute(
                        """
                        INSERT INTO released_slots (operation, slot, patient, created) VALUES (?, ?, ?, ?)
                    """,
                        (change.operation, reference.removeprefix("Slot/"), change.patient, time()),
                    )

    async def activate_notice(self, operation: str) -> str | None:
        return await asyncio.to_thread(self._activate_notice, operation)

    def _activate_notice(self, operation: str) -> str | None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT recipient, trace_id, status FROM appointment_notices WHERE operation=?",
                (operation,),
            ).fetchone()
            if not row or row[2] != "awaiting_commit":
                return row[2] if row else None
            recipient, trace_id, _ = row
            status = "captured"
            if not recipient:
                status = "needs_address"
            elif self.mail.host:
                status = (
                    "queued"
                    if recipient.casefold() in self.mail.allowed
                    else "recipient_unavailable"
                )
            db.execute(
                "UPDATE appointment_notices SET status=? WHERE operation=?", (status, operation)
            )
            if status == "needs_address":
                db.execute(
                    "INSERT OR IGNORE INTO staff_work (id, trace_id, reason, context, updated) VALUES (?, ?, 'email_address_needed', ?, ?)",
                    (f"mail-{operation}", trace_id, json.dumps({"operation": operation}), time()),
                )
            elif status == "recipient_unavailable":
                self._delivery_work(db, operation, trace_id, status)
            return status

    async def reconcile_notices(self, scheduling: "Scheduling") -> None:
        for operation in await asyncio.to_thread(self._pending_notices):
            if not await scheduling.booked_for_operation(operation):
                await asyncio.to_thread(self._supersede_notice, operation)
                continue
            await self.activate_notice(operation)

    def _supersede_notice(self, operation: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE appointment_notices SET status='superseded' WHERE operation=? AND status='awaiting_commit'",
                (operation,),
            )

    def _pending_notices(self) -> list[str]:
        with closing(self.connect()) as db:
            return [
                row[0]
                for row in db.execute("""
                    SELECT n.operation FROM appointment_notices n
                    JOIN mutations m ON m.operation=n.operation
                    WHERE n.status='awaiting_commit' AND m.status='committed'
                    ORDER BY n.created LIMIT 50
                """)
            ]

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def request_notice(self, token: str, address: str, scheduling: "Scheduling") -> str:
        recipient = email_address(address)
        if recipient is None:
            return "invalid_address"
        if self.mail.host and recipient.casefold() not in self.mail.allowed:
            return "recipient_unavailable"
        token_hash = self._hash(token)
        operation = await asyncio.to_thread(self._notice_operation, token_hash)
        if not operation or not await scheduling.booked_for_operation(operation):
            return "unavailable"
        return await asyncio.to_thread(self._queue_notice, token_hash, recipient)

    def _notice_operation(self, token_hash: str) -> str | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT operation FROM appointment_notices WHERE token_hash=? AND created>=?",
                (token_hash, time() - NOTICE_SECONDS),
            ).fetchone()
            return row[0] if row else None

    def _queue_notice(self, token_hash: str, recipient: str) -> str:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            status = "queued" if self.mail.host else "captured"
            row = db.execute(
                """
                SELECT status, recipient FROM appointment_notices WHERE token_hash=? AND created>=?
            """,
                (token_hash, time() - NOTICE_SECONDS),
            ).fetchone()
            if not row:
                return "unavailable"
            if row[0] != "available":
                return row[0] if row[1] == recipient else "already_requested"
            db.execute(
                "UPDATE appointment_notices SET status=?, recipient=? WHERE token_hash=?",
                (status, recipient, token_hash),
            )
            return status

    async def deliver(self, scheduling: "Scheduling") -> None:
        if not self.mail.host:
            return
        operation = await asyncio.to_thread(self._next_delivery)
        if operation is not None:
            still_booked = await scheduling.booked_for_operation(operation)
            await asyncio.to_thread(self._deliver, operation, still_booked)

    def _next_delivery(self) -> str | None:
        with closing(self.connect()) as db:
            row = db.execute("""
                SELECT operation FROM appointment_notices WHERE status='queued' ORDER BY created LIMIT 1
            """).fetchone()
            return row[0] if row else None

    def _deliver(self, operation: str, still_booked: bool) -> None:
        with closing(self.connect()) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            notice = db.execute(
                "SELECT * FROM appointment_notices WHERE status='queued' AND operation=?",
                (operation,),
            ).fetchone()
            if notice is None:
                return
            db.execute(
                "UPDATE appointment_notices SET status=? WHERE operation=?",
                ("sending" if still_booked else "superseded", operation),
            )
        if not still_booked:
            return
        status = "sent"
        try:
            self.mail.send(dict(notice))
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException):
            status = "failed"
        except (OSError, smtplib.SMTPException):
            status = "delivery_unknown"
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE appointment_notices SET status=? WHERE operation=?",
                (status, notice["operation"]),
            )
            if status != "sent":
                self._delivery_work(db, notice["operation"], notice["trace_id"], status)

    @staticmethod
    def _delivery_work(
        db: sqlite3.Connection, operation: str, trace_id: str | None, status: str
    ) -> None:
        context = json.dumps({"operation": operation, "delivery": status})
        db.execute(
            "INSERT OR IGNORE INTO staff_work (id, trace_id, reason, context, updated) VALUES (?, ?, 'email_delivery', ?, ?)",
            (f"mail-{operation}", trace_id, context, time()),
        )

    async def join(self, trace_id: str, patient: str, criteria: dict) -> None:
        await asyncio.to_thread(self._join, trace_id, patient, criteria)

    def _join(self, trace_id: str, patient: str, criteria: dict) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """
                INSERT OR IGNORE INTO waitlist (id, trace_id, patient, criteria, created)
                VALUES (?, ?, ?, ?, ?)
            """,
                (uuid4().hex, trace_id, patient, json.dumps(criteria, sort_keys=True), time()),
            )

    async def match_waitlist(self, scheduling: "Scheduling") -> None:
        for operation, slot_id, cancelling_patient in await asyncio.to_thread(
            self._pending_releases
        ):
            if not await scheduling.receipt(operation):
                continue
            slot = await scheduling.store.read("Slot", slot_id)
            if not slot or slot["status"] != "free":
                await asyncio.to_thread(self._complete_release, operation, None, cancelling_patient)
                continue
            from switchboard.scheduling import instant

            start = instant(slot["start"]).astimezone(scheduling.clinic.timezone)
            minutes = (instant(slot["end"]) - instant(slot["start"])).total_seconds() / 60
            if start <= scheduling.now():
                await asyncio.to_thread(self._complete_release, operation, None, cancelling_patient)
                continue
            offer = {
                "slot": slot_id,
                "start": start.isoformat(),
                "day": start.date().isoformat(),
                "time": start.strftime("%H:%M"),
                "provider": scheduling.provider_for(slot),
                "location": scheduling.location_for(slot),
                "visit": "first visit" if minutes == 40 else "follow-up",
                "operation": operation,
            }
            await asyncio.to_thread(self._complete_release, operation, offer, cancelling_patient)

    def _pending_releases(self) -> list[tuple[str, str, str]]:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time()
            rows = db.execute(
                """
                SELECT operation, slot, patient FROM released_slots
                WHERE processed=0 AND created>=? AND (checked IS NULL OR checked<?)
                ORDER BY COALESCE(checked, created) LIMIT 20
            """,
                (now - WORK_SECONDS, now - 10),
            ).fetchall()
            db.executemany(
                "UPDATE released_slots SET checked=? WHERE operation=?",
                [(now, row[0]) for row in rows],
            )
            return rows

    def _complete_release(
        self, operation: str, offer: dict | None, cancelling_patient: str
    ) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE released_slots SET processed=1 WHERE operation=? AND processed=0",
                (operation,),
            )
            if changed.rowcount != 1 or offer is None:
                return
            for item, trace_id, patient, raw in db.execute(
                """
                SELECT id, trace_id, patient, criteria FROM waitlist
                WHERE status='waiting' AND patient!=? AND created>=? ORDER BY created, id
            """,
                (cancelling_patient, time() - WORK_SECONDS),
            ):
                criteria = json.loads(raw)
                if any(
                    criteria.get(key) and criteria[key] != offer[key]
                    for key in ("visit", "provider", "location", "day")
                ):
                    continue
                if (
                    criteria.get("eligible_providers")
                    and offer["provider"] not in criteria["eligible_providers"]
                ):
                    continue
                if criteria.get("days") and offer["day"] not in criteria["days"]:
                    continue
                if week := criteria.get("week"):
                    start_of_week = date.fromisoformat(week)
                    if (
                        not start_of_week
                        <= date.fromisoformat(offer["day"])
                        < start_of_week + timedelta(days=7)
                    ):
                        continue
                if criteria.get("period") and (
                    (offer["time"] < "12:00") != (criteria["period"] == "morning")
                ):
                    continue
                if criteria.get("requested_time") and offer["time"] != criteria["requested_time"]:
                    continue
                if criteria.get("after") and offer["time"] < criteria["after"]:
                    continue
                if criteria.get("before") and offer["time"] > criteria["before"]:
                    continue
                db.execute("UPDATE waitlist SET status='offered' WHERE id=?", (item,))
                context = {**offer, "patient_reference": f"Patient/{patient}"}
                db.execute(
                    """
                    INSERT INTO staff_work (id, trace_id, reason, context, updated) VALUES (?, ?, ?, ?, ?)
                """,
                    (f"waitlist-{item}", trace_id, "waitlist_offer", json.dumps(context), time()),
                )
                break

    async def purge(self) -> None:
        await asyncio.to_thread(self._purge)

    def _purge(self) -> None:
        with closing(self.connect()) as db, db:
            now = time()
            db.execute(
                "DELETE FROM appointment_notices WHERE created<? AND status='available'",
                (now - NOTICE_SECONDS,),
            )
            db.execute("DELETE FROM appointment_notices WHERE created<?", (now - WORK_SECONDS,))
            db.execute(
                "DELETE FROM staff_work WHERE updated<? AND status='resolved'",
                (now - WORK_SECONDS,),
            )
            db.execute(
                "UPDATE waitlist SET status='expired' WHERE created<? AND status='waiting'",
                (now - WORK_SECONDS,),
            )
            db.execute("DELETE FROM released_slots WHERE created<?", (now - WORK_SECONDS,))
