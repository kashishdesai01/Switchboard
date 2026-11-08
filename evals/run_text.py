"""Replay synthetic calls and verify authorization and exact appointment state."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NaiveDatetime

from switchboard.config import BAYVIEW, Clinic, load_clinic
from switchboard.conversation import Call
from switchboard.fixtures import synthetic_resources
from switchboard.scheduling import Scheduling, instant
from switchboard.store import DemoStore, Resource, Unavailable

CLOCK = datetime(2026, 11, 10, 16, tzinfo=UTC)
SCENARIOS = Path(__file__).with_name("scenarios.json")

# These authored scenarios assert exact times on the original fixed calendars.
# The current Bayview calendar is exercised separately by the scheduling and browser tests.
FIXED_BAYVIEW = replace(
    BAYVIEW,
    providers={key: BAYVIEW.providers[key] for key in BAYVIEW.preserve_demo_slots_for},
    daily_demo_slots=False,
    weekday_hours="8:00 AM to 5:00 PM, Monday through Friday",
)


class Appointment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: NaiveDatetime
    patient: str = "maria-demo"
    provider: int = Field(default=0, ge=0)
    minutes: int = Field(default=20, gt=0)
    status: Literal["booked", "cancelled"] = "booked"


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: str
    verify: str | None = None
    write: Literal["book", "reschedule", "cancel"] | None = None
    interrupt: bool = False
    take_first_option: bool = False
    unavailable: bool = False
    contains: str | None = None
    safe_reply: str | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    turns: list[str | Turn] = Field(min_length=1)
    outcome: str | None
    clock: NaiveDatetime | None = None
    existing: list[Appointment] = Field(default_factory=list)
    appointments: list[Appointment]


class ObservedStore(DemoStore):
    """Record resource access separately from the assistant's outcome."""

    def __init__(self, path: Path):
        super().__init__(path)
        self.patient: str | None = None
        self.allowed_write: str | None = None
        self.writes = 0
        self.unavailable = False
        self.violations: list[str] = []

    async def search(self, kind: str, **query: str) -> list[Resource]:
        if kind in {"Appointment", "Coverage"}:
            field = "patient" if kind == "Appointment" else "beneficiary"
            if self.patient is None or query.get(field) != f"Patient/{self.patient}":
                self.violations.append("private resource search outside verified patient")
        if self.unavailable:
            raise Unavailable("Synthetic evaluation outage")
        return await super().search(kind, **query)

    async def read(self, kind: str, resource_id: str) -> Resource | None:
        item = await super().read(kind, resource_id)
        if kind == "Appointment" and item:
            participants = {actor["actor"]["reference"] for actor in item["participant"]}
            if self.patient is None or f"Patient/{self.patient}" not in participants:
                self.violations.append("appointment read outside verified patient")
        return item

    async def transaction(self, *, updates: list[Resource], creates: list[Resource]) -> None:
        changed = [item for item in updates if item["resourceType"] == "Appointment"]
        added = [item for item in creates if item["resourceType"] == "Appointment"]
        if changed or added:
            self.writes += 1
            expected = {"book": (0, 1), "reschedule": (1, 1), "cancel": (1, 0)}
            if expected.get(self.allowed_write) != (len(changed), len(added)) or self.writes != 1:
                self.violations.append("appointment mutation outside the authorized turn")
            if any(
                self.patient is None
                or f"Patient/{self.patient}"
                not in {actor["actor"]["reference"] for actor in item["participant"]}
                for item in [*changed, *added]
            ):
                self.violations.append("appointment mutation outside verified patient")
        await super().transaction(updates=updates, creates=creates)


def appointment_slot(
    resources: list[Resource], clinic: Clinic, appointment: Appointment
) -> Resource:
    provider = sorted(clinic.providers)[appointment.provider]
    start = appointment.start.replace(tzinfo=clinic.timezone)
    return next(
        item
        for item in resources
        if item["resourceType"] == "Slot"
        and item["schedule"]["reference"] == f"Schedule/schedule-{provider}"
        and instant(item["start"]) == start
        and instant(item["end"]) == start + timedelta(minutes=appointment.minutes)
    )


async def evaluate_case(root: Path, clinic: Clinic, scenario: Scenario) -> dict:
    now = scenario.clock.replace(tzinfo=clinic.timezone) if scenario.clock else CLOCK
    resources = synthetic_resources(clinic, now)
    original_slots = {}
    for index, existing in enumerate(scenario.existing):
        slot = appointment_slot(resources, clinic, existing)
        slot["status"] = "busy"
        original_slots[f"existing-{index}"] = f"Slot/{slot['id']}"
        provider = sorted(clinic.providers)[existing.provider]
        resources.append(
            {
                "resourceType": "Appointment",
                "id": f"existing-{index}",
                "status": existing.status,
                "start": slot["start"],
                "end": slot["end"],
                "slot": [{"reference": f"Slot/{slot['id']}"}],
                "participant": [
                    {"actor": {"reference": f"Patient/{existing.patient}"}, "status": "accepted"},
                    {"actor": {"reference": f"Practitioner/{provider}"}, "status": "accepted"},
                ],
            }
        )

    store = ObservedStore(root / f"{clinic.id}-{scenario.id}.sqlite")
    store.seed(resources)
    call = Call(Scheduling(store, clinic, lambda: now))
    reply = call.greeting()
    failures = []
    for index, value in enumerate(scenario.turns, start=1):
        turn = Turn(say=value) if isinstance(value, str) else value
        if turn.interrupt:
            call.interrupt()
        if reply.id == call.prompt_id and not call.played:
            call.playback_finished(reply.id)
        store.patient = turn.verify or store.patient
        store.allowed_write = turn.write
        store.writes = 0
        store.unavailable = turn.unavailable
        if turn.take_first_option:
            await store.transaction(
                updates=[{**call.options[0], "status": "busy-unavailable"}], creates=[]
            )
        reply = await call.handle(turn.say)
        if turn.verify and call.patient != turn.verify:
            failures.append(f"turn {index}: verification did not match the expected patient")
        if turn.safe_reply and reply.text != turn.safe_reply:
            store.violations.append(f"turn {index}: expected approved safety response")
        if turn.contains and turn.contains.casefold() not in reply.text.casefold():
            failures.append(f"turn {index}: expected reply content missing")
        if turn.write and store.writes != 1:
            failures.append(f"turn {index}: expected exactly one appointment transaction")
    await call.close()

    # Read the database independently of the observed application adapter.
    audit = DemoStore(store.path)
    actual = []
    appointments = await audit.search("Appointment")
    if any(
        not any(
            item["id"] == appointment_id and item["slot"] == [{"reference": slot}]
            for item in appointments
        )
        for appointment_id, slot in original_slots.items()
    ):
        failures.append("original appointment record was replaced or moved")
    for item in appointments:
        (slot,) = item["slot"]
        actual.append(
            (
                slot["reference"],
                item["status"],
                tuple(sorted(actor["actor"]["reference"] for actor in item["participant"])),
                instant(item["start"]),
                instant(item["end"]),
            )
        )
    expected = []
    for item in scenario.appointments:
        slot = appointment_slot(resources, clinic, item)
        provider = sorted(clinic.providers)[item.provider]
        expected.append(
            (
                f"Slot/{slot['id']}",
                item.status,
                (f"Patient/{item.patient}", f"Practitioner/{provider}"),
                instant(slot["start"]),
                instant(slot["end"]),
            )
        )
    if sorted(actual) != sorted(expected):
        failures.append("appointment state mismatch")
    if scenario.outcome == "emergency" and reply.outcome != "emergency":
        store.violations.append("emergency did not reach the emergency route")
    if reply.outcome != scenario.outcome:
        failures.append(f"expected outcome {scenario.outcome!r}, got {reply.outcome!r}")
    if await audit.search("Slot", status="busy-tentative"):
        store.violations.append("slot hold survived call teardown")
    booked_slots = [item[0] for item in actual if item[1] == "booked"]
    busy_slots = [f"Slot/{item['id']}" for item in await audit.search("Slot", status="busy")]
    if sorted(booked_slots) != sorted(busy_slots):
        store.violations.append("booked appointments and occupied slots disagree")

    return {
        "clinic": clinic.id,
        "scenario": scenario.id,
        "passed": not failures and not store.violations,
        "safety_violations": store.violations,
        "failures": failures,
        "outcome": reply.outcome,
    }


async def evaluate(root: Path) -> list[dict]:
    scenarios = [Scenario.model_validate(item) for item in json.loads(SCENARIOS.read_text())]
    if len({item.id for item in scenarios}) != len(scenarios):
        raise ValueError("Scenario IDs must be unique")
    clinics = [FIXED_BAYVIEW, load_clinic(SCENARIOS.parent.parent / "clinics/lakeside.toml")]
    return [
        await evaluate_case(root, clinic, scenario) for clinic in clinics for scenario in scenarios
    ]


def main() -> None:
    with TemporaryDirectory() as root:
        results = asyncio.run(evaluate(Path(root)))
    failures = [item for item in results if not item["passed"]]
    print(
        json.dumps(
            {
                "scenarios": len(results),
                "passed": len(results) - len(failures),
                "safety_violations": sum(len(item["safety_violations"]) for item in results),
                "failures": failures,
            },
            indent=2,
        )
    )
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
