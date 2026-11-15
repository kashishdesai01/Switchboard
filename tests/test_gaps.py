"""Gaps found in the 2026-11-15 review, locked in as strict expected failures.

Each gap test asserts the target behavior, so it fails today and starts passing
once the phase that closes the gap lands. That phase removes the xfail mark.
``raises=AssertionError`` keeps harness errors visible: only a failed behavior
assertion counts as the expected failure. Harness preconditions use
``require`` so that a broken setup fails the test instead of hiding in xfail.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from switchboard.config import BAYVIEW
from switchboard.conversation import Call, Reply
from switchboard.fixtures import synthetic_resources
from switchboard.frontdesk import FrontDesk, MailSettings
from switchboard.operations import Operations, Trace
from switchboard.scheduling import Scheduling, instant
from switchboard.store import DemoStore, Resource

# 08:00 on Tuesday, November 10 at the clinic. Fixed so the generated slots never change.
CLOCK = datetime(2026, 11, 10, 16, tzinfo=UTC)
TODAY = CLOCK.astimezone(BAYVIEW.timezone).date()
MARIA = ("Maria Lopez", "July 2 1984")
ORDINALS = ("one", "two", "three")


def gap(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)


class HarnessError(RuntimeError):
    """A broken test setup, as opposed to the behavior under test."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise HarnessError(message)


def spoken_day(day: date) -> str:
    return f"{day:%B} {day.day}"


def clock_time(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def local_start(item: Resource) -> datetime:
    return instant(item["start"]).astimezone(BAYVIEW.timezone)


def is_follow_up(slot: Resource) -> bool:
    return instant(slot["end"]) - instant(slot["start"]) == timedelta(minutes=20)


def maria_appointment(slot: Resource, index: int) -> Resource:
    provider = slot["schedule"]["reference"].removeprefix("Schedule/schedule-")
    return {
        "resourceType": "Appointment",
        "id": f"existing-{index}",
        "status": "booked",
        "start": slot["start"],
        "end": slot["end"],
        "slot": [{"reference": f"Slot/{slot['id']}"}],
        "participant": [
            {"actor": {"reference": "Patient/maria-demo"}, "status": "accepted"},
            {"actor": {"reference": f"Practitioner/{provider}"}, "status": "accepted"},
        ],
    }


@dataclass
class Session:
    call: Call
    trace: Trace
    reply: Reply
    replies: list[Reply] = field(default_factory=list)

    async def say(self, text: str) -> Reply:
        # The browser acknowledges that the previous reply finished playing.
        self.call.playback_finished(self.reply.id)
        self.reply = await self.call.handle(text)
        self.replies.append(self.reply)
        return self.reply

    async def verify_maria(self) -> Reply:
        """Answer the name, date of birth and read-back prompts."""
        name, dob = MARIA
        await self.say(name)
        await self.say(dob)
        return await self.say("yes")

    async def pick(self, slot_id: str) -> Reply:
        ids = [item["id"] for item in self.call.options]
        require(slot_id in ids, f"slot {slot_id} was not offered: {ids}")
        return await self.say(ORDINALS[ids.index(slot_id)])


class Clinic:
    """A seeded SQLite demo clinic with the front desk enabled."""

    def __init__(self, root: Path, *, existing: int = 0):
        self.resources = synthetic_resources(BAYVIEW, CLOCK)
        self.slots = [item for item in self.resources if item["resourceType"] == "Slot"]
        # Existing appointments are follow-ups well outside the 24 hour notice window.
        for index in range(existing):
            day = TODAY + timedelta(days=3 + index)
            slot = min(
                (s for s in self.slots if local_start(s).date() == day and is_follow_up(s)),
                key=local_start,
            )
            slot["status"] = "busy"
            self.resources.append(maria_appointment(slot, index))
        self.store = DemoStore(root / "s.sqlite")
        self.store.seed(self.resources)
        self.scheduling = Scheduling(self.store, BAYVIEW, now=lambda: CLOCK)
        self.ops = Operations(root / "ops.sqlite", "test")
        self.frontdesk = FrontDesk(self.ops, MailSettings())
        self.ops.frontdesk = self.frontdesk

    def session(self, *, proxy_booking_allowed: bool | None = None) -> Session:
        trace = self.ops.start("test")
        call = Call(self.scheduling, None, trace, allow_registration=True, frontdesk=self.frontdesk)
        if proxy_booking_allowed is not None:
            call.proxy_booking_allowed = proxy_booking_allowed
        return Session(call, trace, call.greeting())

    async def booked(self) -> list[Resource]:
        return [
            item for item in await self.store.search("Appointment") if item["status"] == "booked"
        ]

    async def book_maria(self, request: str, slot_id: str | None = None) -> Resource:
        session = self.session()
        await session.say(request)
        await session.say("For me.")
        reply = await session.verify_maria()
        require(session.call.options, f"no options offered: {reply.text}")
        await (session.pick(slot_id) if slot_id else session.say("one"))
        reply = await session.say("yes")
        require(reply.outcome == "booked", f"booking failed: {reply.text}")
        (appointment,) = await self.booked()
        return appointment


def run(coroutine) -> None:
    asyncio.run(coroutine)


def asks_permission(reply: Reply) -> bool:
    return "permission" in reply.text.lower()


# Controls: these pass today and prove the harness drives real calls.


def test_control_self_booking(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        await session.say("book a follow-up")
        await session.say("For me.")
        reply = await session.verify_maria()
        assert "Option 1" in reply.text
        await session.say("one")
        reply = await session.say("yes")
        assert reply.outcome == "booked"
        (appointment,) = await clinic.booked()
        assert appointment["participant"][0]["actor"]["reference"] == "Patient/maria-demo"

    run(scenario())


def test_control_reschedule(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        original = await clinic.book_maria(
            f"book a follow-up on {spoken_day(TODAY + timedelta(days=6))}"
        )
        session = clinic.session()
        await session.say("reschedule my appointment")
        reply = await session.verify_maria()
        assert "Option 1" in reply.text
        await session.say("one")
        require(session.call.options, "no new times offered")
        new_start = session.call.options[0]["start"]
        await session.say("one")
        reply = await session.say("yes")
        assert reply.outcome == "rescheduled"
        (moved,) = await clinic.booked()
        assert moved["start"] == new_start != original["start"]

    run(scenario())


# Proxy and recipient phrasing


@gap("'someone else' is not recognized as a proxy answer; the recipient question repeats")
def test_someone_else_asks_permission(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("book a follow-up")
        require("someone else" in reply.text, f"unexpected prompt: {reply.text}")
        reply = await session.say("someone else")
        assert asks_permission(reply), reply.text

    run(scenario())


@gap("a named recipient ('it's for John Smith') is not recognized; the question repeats")
def test_named_recipient_asks_permission(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("book a follow-up")
        require("someone else" in reply.text, f"unexpected prompt: {reply.text}")
        reply = await session.say("it's for John Smith")
        assert asks_permission(reply), reply.text

    run(scenario())


@gap("'needs an appointment' is not a booking verb, so the proxy request is not understood")
def test_father_needs_appointment_asks_permission(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("my father needs an appointment")
        assert asks_permission(reply), reply.text

    run(scenario())


@gap("'I'm calling for my father' hands off to staff instead of asking for permission")
def test_calling_for_father_asks_permission(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("I'm calling for my father, he needs an appointment")
        assert asks_permission(reply), reply.text
        assert reply.outcome is None

    run(scenario())


@gap("a possessive proxy cancel ('my mom's appointment') goes straight to identity questions")
def test_cancel_moms_appointment_asks_permission(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("I need to cancel my mom's appointment")
        assert reply.text != "What name should I use?"
        assert asks_permission(reply), reply.text

    run(scenario())


@gap("'cancel the appointment for my mom' hands off instead of asking for permission")
def test_cancel_for_mom_asks_permission(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("I need to cancel the appointment for my mom")
        assert asks_permission(reply), reply.text
        assert reply.outcome is None

    run(scenario())


@gap("the staff work item for a proxy cancel records request 'book' instead of 'cancel'")
def test_proxy_cancel_work_item_records_cancel(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session(proxy_booking_allowed=False)
        await session.say("I need to cancel the appointment for my mom")
        # Accept any offer to pass the request to the front desk.
        for _ in range(3):
            if session.call.stage == "closed" or session.trace.work:
                break
            await session.say("yes")
        require(session.trace.work, "no staff work item was created")
        _reason, context = session.trace.work
        assert context["request"] == "cancel", context

    run(scenario())


# Safety


@gap("an answer to the stroke clarifying question that also asks to book loops forever")
def test_past_stroke_then_follow_up_continues_booking(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("stroke")
        require("right now" in reply.text, f"no clarifying question: {reply.text}")
        reply = await session.say("no, it was in the past, I need to book a follow-up")
        assert "for you or for someone else" in reply.text, reply.text
        assert reply.outcome is None

    run(scenario())


@pytest.mark.parametrize(
    "report",
    [
        pytest.param(
            "I don't want to live anymore",
            marks=gap("suicidal ideation phrasing is not detected"),
        ),
        pytest.param(
            "my face is swelling up and I can't swallow",
            marks=gap("facial swelling with trouble swallowing is not detected"),
        ),
        pytest.param(
            "I'm having an allergic reaction and my throat feels tight",
            marks=gap("anaphylaxis phrasing is not detected"),
        ),
        pytest.param(
            "I fell and the bone is sticking out of my leg",
            marks=gap("an open fracture is not detected"),
        ),
    ],
)
def test_missed_emergency(report, tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say(report)
        assert reply.outcome == "emergency", reply.text

    run(scenario())


@gap("suicidal ideation does not get the 988 crisis line")
def test_crisis_reply_gives_988(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("I don't want to live anymore")
        assert "988" in reply.text, reply.text

    run(scenario())


@gap("the greeting does not tell callers to hang up and dial 911 in an emergency")
def test_greeting_mentions_911(tmp_path):
    session = Clinic(tmp_path).session()
    assert "911" in session.reply.text, session.reply.text


# Scheduling and call flow


def simultaneous_follow_up(clinic: Clinic) -> tuple[Resource, Resource]:
    """Two free follow-ups with different providers at the same time, days ahead."""
    starts: dict[datetime, list[Resource]] = defaultdict(list)
    for slot in clinic.slots:
        if slot["status"] == "free" and is_follow_up(slot):
            starts[local_start(slot)].append(slot)
    start = min(
        moment
        for moment, slots in starts.items()
        if len(slots) >= 2 and moment.date() >= TODAY + timedelta(days=3)
    )
    first, second = starts[start][:2]
    return first, second


@gap("offers include times that overlap the patient's own booked appointment")
def test_no_double_booking(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        target, _other = simultaneous_follow_up(clinic)
        start = local_start(target)
        request = f"book a follow-up on {spoken_day(start.date())} at {clock_time(start)}"
        existing = await clinic.book_maria(request, target["id"])
        session = clinic.session()
        await session.say(request)
        await session.say("For me.")
        await session.verify_maria()
        require(session.call.options, "no options offered on the second call")
        booked_start, booked_end = instant(existing["start"]), instant(existing["end"])
        overlapping = [
            option["id"]
            for option in session.call.options
            if instant(option["start"]) < booked_end and booked_start < instant(option["end"])
        ]
        assert overlapping == [], overlapping

    run(scenario())


@gap("text mode lists two appointments and cannot page to the third")
def test_third_appointment_reachable_in_text(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=3)
        session = clinic.session()
        await session.say("reschedule my appointment")
        reply = await session.verify_maria()
        require(session.call.stage != "closed", f"call ended: {reply.text}")
        third = max(await clinic.booked(), key=local_start)
        description = session.call.describe(third)
        shown = [reply.text]
        if description not in reply.text:
            shown.append((await session.say("more appointments")).text)
        assert any(description in text for text in shown), shown

    run(scenario())


@gap("a name given in the opening sentence is ignored and asked for again")
def test_name_in_opener_is_used(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say("hi this is Maria Lopez, I want to move my appointment")
        assert "what name should i use" not in reply.text.lower(), reply.text

    run(scenario())


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("what are your hours?", marks=gap("the call closes after the hours answer")),
        pytest.param(
            "do you accept Aetna insurance?",
            marks=gap("the call closes after the insurance answer"),
        ),
    ],
)
def test_call_continues_after_info(question, tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        await session.say(question)
        assert session.call.stage != "closed"
        reply = await session.say("book an appointment")
        assert "for you or for someone else" in reply.text, reply.text

    run(scenario())


@gap("the call closes when there are no upcoming appointments to change")
def test_call_continues_after_no_appointments(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        require(not await clinic.booked(), "expected no appointments")
        session = clinic.session()
        await session.say("reschedule my appointment")
        await session.verify_maria()
        assert session.call.stage != "closed"
        reply = await session.say("book an appointment")
        assert reply.outcome is None, reply.text
        # Booking continues: either the recipient question or, with identity kept, times.
        assert "for you or for someone else" in reply.text or "Option 1" in reply.text, reply.text

    run(scenario())
