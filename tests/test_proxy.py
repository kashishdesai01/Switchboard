"""Calls where the caller is not the patient: book, reschedule and cancel."""

import pytest
from test_gaps import Clinic, run

from switchboard.conversation import Party


async def verify_patient(session) -> None:
    """Give the patient's identity, checking the prompts refer to the patient."""
    reply = await session.say("Maria Lopez")
    assert "patient's date of birth" in reply.text, reply.text
    reply = await session.say("July 2 1984")
    assert "Maria Lopez" in reply.text
    return await session.say("yes")


def test_cancel_moms_appointment_end_to_end(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session()
        reply = await session.say("I need to cancel my mom's appointment")
        assert reply.text == "Do you have permission to cancel this patient's appointment?"
        reply = await session.say("yes")
        assert reply.text == "What's the patient's full name?"
        reply = await verify_patient(session)
        assert reply.text.startswith("The patient has one upcoming appointment:"), reply.text
        reply = await session.say("one")
        assert reply.text.startswith("Cancel the patient's appointment on"), reply.text
        reply = await session.say("yes")
        assert reply.outcome == "cancelled"
        assert reply.text.startswith("The patient's appointment was cancelled.")
        assert await clinic.booked() == []
        assert session.call.party == Party("other", "mom")

    run(scenario())


def test_reschedule_for_father_end_to_end(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        (original,) = await clinic.booked()
        session = clinic.session()
        reply = await session.say("reschedule the appointment for my father")
        assert reply.text == "Do you have permission to change this patient's appointment?"
        await session.say("yes")
        reply = await verify_patient(session)
        assert reply.text.startswith("The patient has one upcoming appointment:"), reply.text
        await session.say("one")
        assert session.call.options, "no new times offered"
        new_start = session.call.options[0]["start"]
        reply = await session.say("one")
        # No email question: confirmation email is skipped for proxy calls.
        assert reply.text.startswith("Move the patient's appointment from"), reply.text
        reply = await session.say("yes")
        assert reply.outcome == "rescheduled"
        assert reply.text.startswith("The patient is rescheduled for")
        assert "confirmation" not in reply.text
        (moved,) = await clinic.booked()
        assert moved["start"] == new_start != original["start"]

    run(scenario())


def test_permission_no_makes_no_changes(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session()
        await session.say("I need to cancel the appointment for my mom")
        reply = await session.say("no")
        assert reply.outcome == "declined"
        assert reply.text.startswith("Okay. I haven't made any changes.")
        assert len(await clinic.booked()) == 1
        assert session.trace.work is None

    run(scenario())


def test_unclear_permission_reasks_then_hands_off(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        await session.say("cancel my dad's appointment")
        reply = await session.say("well")
        assert "permission to cancel this patient's appointment" in reply.text
        assert reply.outcome is None
        await session.say("hmm")
        reply = await session.say("I guess")
        assert reply.outcome == "handoff"
        _reason, context = session.trace.work
        assert context["request"] == "cancel"
        assert context["relation"] == "dad"

    run(scenario())


@pytest.mark.parametrize(
    ("opening", "kind", "relation"),
    [
        ("book a follow-up for my son", "book", "son"),
        ("reschedule the appointment for my father", "reschedule", "father"),
        ("I need to cancel my mom's appointment", "cancel", "mom"),
    ],
)
def test_fhir_mode_sends_proxy_request_to_front_desk(tmp_path, opening, kind, relation):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session(proxy_allowed=False)
        reply = await session.say(opening)
        assert "front desk" in reply.text and reply.outcome is None
        reply = await session.say("yes")
        assert reply.outcome == "handoff"
        reason, context = session.trace.work
        assert reason == "staff_requested"
        assert context["request"] == kind
        assert context["relation"] == relation
        assert context["patient"] == "other"
        assert len(await clinic.booked()) == 1

    run(scenario())


def test_named_patient_is_read_back(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        await session.say("book a follow-up")
        reply = await session.say("it's for John Smith")
        assert reply.text == "Do you have permission to book a visit for this patient?"
        reply = await session.say("yes")
        assert "I heard the patient's name as John Smith. Is that correct?" in reply.text
        assert session.call.stage == "check_name"
        reply = await session.say("yes")
        assert reply.text == "What's the patient's date of birth?"

    run(scenario())


def test_emergency_during_permission_question(tmp_path):
    async def scenario():
        session = Clinic(tmp_path, existing=1).session()
        await session.say("I need to cancel the appointment for my mom")
        assert session.call.stage == "proxy_permission"
        reply = await session.say("she has crushing chest pain right now")
        assert reply.outcome == "emergency"
        reason, _context = session.trace.work
        assert reason == "emergency"

    run(scenario())


def test_explicit_staff_request_beats_proxy_flow(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        reply = await session.say(
            "I want to speak to a person about cancelling my mom's appointment"
        )
        assert reply.outcome == "handoff"
        assert "permission" not in reply.text

    run(scenario())


def test_cancel_my_appointment_is_unchanged(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session()
        reply = await session.say("cancel my appointment")
        assert reply.text == "What name should I use?"
        reply = await session.verify_maria()
        assert reply.text.startswith("You have one upcoming appointment:"), reply.text
        reply = await session.say("one")
        assert reply.text.startswith("Cancel your appointment on")
        reply = await session.say("yes")
        assert reply.text.startswith("Your appointment was cancelled.")
        assert session.call.party.patient == "self"
        assert await clinic.booked() == []

    run(scenario())
