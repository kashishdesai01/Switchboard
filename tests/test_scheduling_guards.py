"""Double booking and paging through existing appointments."""

from datetime import timedelta

import pytest
from test_gaps import (
    TODAY,
    Clinic,
    clock_time,
    local_start,
    require,
    run,
    simultaneous_follow_up,
    spoken_day,
)

from switchboard.scheduling import Change, instant
from switchboard.store import Conflict, Resource

MARIA_ID = "maria-demo"


def overlaps(slot: Resource, other: Resource) -> bool:
    return instant(slot["start"]) < instant(other["end"]) and instant(other["start"]) < instant(
        slot["end"]
    )


def existing_day(index: int = 0):
    return TODAY + timedelta(days=3 + index)


async def maria_options(clinic: Clinic, **kwargs) -> list[Resource]:
    return await clinic.scheduling.options(
        "follow-up", existing_day(), limit=100, patient=MARIA_ID, **kwargs
    )


def test_options_exclude_overlapping_slots(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        (existing,) = await clinic.booked()
        unfiltered = await clinic.scheduling.options("follow-up", existing_day(), limit=100)
        require(
            any(overlaps(slot, existing) for slot in unfiltered),
            "expected another provider free at the booked time",
        )
        filtered = await maria_options(clinic)
        assert filtered
        assert not any(overlaps(slot, existing) for slot in filtered)

    run(scenario())


def test_rescheduled_appointment_is_ignored(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        (existing,) = await clinic.booked()
        options = await maria_options(clinic, ignore=existing["id"])
        assert any(overlaps(slot, existing) for slot in options)

    run(scenario())


def test_commit_rejects_overlapping_booking(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        first, second = simultaneous_follow_up(clinic)
        scheduling = clinic.scheduling
        holds = [
            await scheduling.hold(await clinic.store.read("Slot", slot["id"]))
            for slot in (first, second)
        ]
        await scheduling.commit(Change(holds[0].operation, "book", MARIA_ID, None, holds[0]))
        with pytest.raises(Conflict, match="Patient already booked"):
            await scheduling.commit(Change(holds[1].operation, "book", MARIA_ID, None, holds[1]))
        assert len(await clinic.booked()) == 1

    run(scenario())


def test_concurrent_booking_for_same_patient_reoffers(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        target, other = simultaneous_follow_up(clinic)
        start = local_start(target)
        session = clinic.session()
        await session.say(f"book a follow-up on {spoken_day(start.date())} at {clock_time(start)}")
        await session.say("For me.")
        await session.verify_maria()
        reply = await session.pick(target["id"])
        require(session.call.change and session.call.change.hold, f"no hold: {reply.text}")

        # Another call books Maria at the same time with a different provider.
        hold = await clinic.scheduling.hold(await clinic.store.read("Slot", other["id"]))
        await clinic.scheduling.commit(Change(hold.operation, "book", MARIA_ID, None, hold))

        reply = await session.say("yes")
        assert reply.outcome is None, reply.text
        assert reply.text.startswith("That time is no longer available."), reply.text
        (booked,) = await clinic.booked()
        assert booked["slot"] == [{"reference": f"Slot/{other['id']}"}]
        assert session.call.options
        assert not any(overlaps(slot, booked) for slot in session.call.options)

    run(scenario())


def test_text_pages_through_four_appointments(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=4)
        appointments = sorted(await clinic.booked(), key=local_start)
        session = clinic.session()
        await session.say("reschedule my appointment")
        reply = await session.verify_maria()
        describe = session.call.describe
        assert reply.text.startswith("Which existing appointment do you want to change?")
        assert all(describe(item) in reply.text for item in appointments[:3]), reply.text
        assert describe(appointments[3]) not in reply.text

        reply = await session.say("more")
        assert reply.text.startswith("Here are more appointments."), reply.text
        assert f"Option 1: {describe(appointments[3])}." in reply.text
        assert "Option 2" not in reply.text

        reply = await session.say("more appointments")
        assert reply.text.startswith("These are the last appointments I found."), reply.text

        await session.say("one")
        assert session.call.old is not None
        assert session.call.old["id"] == appointments[3]["id"]
        assert session.call.stage == "select_slot"

    run(scenario())


def test_single_appointment_confirm_yes(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        (existing,) = await clinic.booked()
        session = clinic.session()
        await session.say("cancel my appointment")
        reply = await session.verify_maria()
        assert reply.text == (
            f"You have one upcoming appointment: {session.call.describe(existing)}. "
            "Do you want to cancel it?"
        )
        reply = await session.say("yes")
        assert reply.text.startswith("Cancel your appointment on"), reply.text
        reply = await session.say("yes")
        assert reply.outcome == "cancelled", reply.text
        assert await clinic.booked() == []

    run(scenario())


def test_single_appointment_confirm_no(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session()
        await session.say("reschedule my appointment")
        await session.verify_maria()
        reply = await session.say("no")
        assert reply.text.startswith("Okay. I haven't made any changes."), reply.text
        assert reply.outcome == "declined"
        assert session.call.old is None
        assert len(await clinic.booked()) == 1

    run(scenario())


@pytest.mark.parametrize("choice", ["one", "option one", "the first one"])
def test_single_appointment_accepts_one(choice, tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        (existing,) = await clinic.booked()
        session = clinic.session()
        await session.say("reschedule my appointment")
        await session.verify_maria()
        reply = await session.say(choice)
        assert session.call.old is not None, reply.text
        assert session.call.old["id"] == existing["id"]
        assert session.call.stage == "select_slot"
        assert "Option 1" in reply.text

    run(scenario())


def test_single_appointment_rejects_other_option(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session()
        await session.say("reschedule my appointment")
        await session.verify_maria()
        reply = await session.say("two")
        assert session.call.old is None
        assert reply.text.startswith("Please say yes or no."), reply.text

    run(scenario())


def test_more_with_single_appointment(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session()
        await session.say("cancel my appointment")
        await session.verify_maria()
        reply = await session.say("more")
        assert reply.text.startswith("These are the last appointments I found."), reply.text
        assert "Do you want to cancel it?" in reply.text

    run(scenario())
