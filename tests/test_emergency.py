"""Emergency handling: a script per triage level, urgent routing, the clarifying
question, the model safety check and urgent staff work."""

import asyncio
import json

import pytest
from test_gaps import Clinic, run

from switchboard.frontdesk import TASK_REASONS
from switchboard.intents import TurnMeaning
from switchboard.safety import (
    CRISIS_SCRIPT,
    EMERGENCY_SCRIPT,
    MODEL_CATEGORY,
    POISONING_SCRIPT,
    URGENT_SCRIPT,
    Level,
    Triage,
    model_level,
    raise_level,
)
from switchboard.scheduling import hold_metadata


async def work_items(clinic: Clinic, *sessions) -> list[dict]:
    for session in sessions:
        await clinic.ops.finish(session.trace)
    return (await clinic.frontdesk.snapshot())["tasks"]


def routed(session, event: str) -> dict:
    (entry,) = [item for item in session.trace.events if item["event"] == event]
    return entry


@pytest.mark.parametrize(
    ("text", "script", "reason", "level", "category"),
    [
        ("I have crushing chest pain right now", EMERGENCY_SCRIPT, "emergency", "EMERGENCY", None),
        ("I want to kill myself", CRISIS_SCRIPT, "crisis", "CRISIS", "suicide_self_harm"),
        (
            "My toddler swallowed some bleach",
            POISONING_SCRIPT,
            "poisoning",
            "POISONING",
            "poisoning",
        ),
    ],
)
def test_each_acute_level_has_its_script_and_work_reason(
    tmp_path, text, script, reason, level, category
):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        reply = await session.say(text)
        assert (reply.text, reply.outcome) == (script, "emergency")
        event = routed(session, "emergency_routed")
        assert event["level"] == level
        if category:
            assert event["category"] == category
        # The trace event records the level and category, never the caller's words.
        assert text not in json.dumps(list(session.trace.events))
        (item,) = await work_items(clinic, session)
        assert item["reason"] == reason
        assert item["urgent"] is True
        assert item["context"]["triage_level"] == level

    run(scenario())


def test_crisis_and_poisoning_scripts_name_the_right_lines():
    assert "988" in CRISIS_SCRIPT and "911" in CRISIS_SCRIPT
    assert "1-800-222-1222" in POISONING_SCRIPT and "911" in POISONING_SCRIPT
    assert {"crisis", "poisoning", "possible_emergency", "urgent_symptoms"} <= TASK_REASONS.keys()


def test_greeting_gives_the_911_notice(tmp_path):
    session = Clinic(tmp_path).session()
    assert "If this is a medical emergency, hang up and call 911." in session.reply.text


def test_urgent_symptoms_hand_off_at_the_start(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        reply = await session.say("I fell and can't put weight on my leg")
        assert (reply.text, reply.outcome) == (URGENT_SCRIPT, "handoff")
        assert routed(session, "urgent_routed")["category"] == "cannot_bear_weight"
        (item,) = await work_items(clinic, session)
        assert item["reason"] == "urgent_symptoms"
        assert item["context"]["category"] == "cannot_bear_weight"
        assert item["context"]["triage_level"] == "URGENT"

    run(scenario())


def test_urgent_symptoms_mid_booking_release_the_hold_and_book_nothing(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        await session.say("book a follow-up")
        await session.say("For me.")
        await session.verify_maria()
        await session.say("one")
        hold = session.call.change.hold
        assert hold is not None
        held = await clinic.store.read("Slot", hold.slot["id"])
        assert hold_metadata(held) is not None
        reply = await session.say("I fell and can't put weight on my leg")
        assert (reply.text, reply.outcome) == (URGENT_SCRIPT, "handoff")
        released = await clinic.store.read("Slot", hold.slot["id"])
        assert hold_metadata(released) is None
        assert session.call.change is None
        # A later "yes" cannot complete the booking.
        assert (await session.say("yes")).outcome == "closed"
        assert await clinic.booked() == []
        (item,) = await work_items(clinic, session)
        assert item["reason"] == "urgent_symptoms"

    run(scenario())


def test_two_unclear_answers_to_the_clarifying_question_escalate(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        assert "happening right now" in (await session.say("Stroke.")).text
        assert "happening right now" in (await session.say("Hmm")).text
        reply = await session.say("I'm not sure")
        assert (reply.text, reply.outcome) == (EMERGENCY_SCRIPT, "emergency")
        (item,) = await work_items(clinic, session)
        assert item["reason"] == "possible_emergency"
        assert item["urgent"] is True

    run(scenario())


def test_clear_answers_to_the_clarifying_question_still_route(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        yes = clinic.session()
        await yes.say("Stroke.")
        assert (await yes.say("Yes")).outcome == "emergency"
        no = clinic.session()
        await no.say("Stroke.")
        assert "How can I help" in (await no.say("No.")).text

    run(scenario())


class SafetyModel:
    """A fake interpreter with a model safety check and no network."""

    def __init__(self, level: Level | None = None, *, delay: float = 0.0, error: bool = False):
        self.level = level
        self.delay = delay
        self.error = error
        self.seen: list[str] = []

    async def interpret_turn(self, context, actions):
        return TurnMeaning(action="unclear", value="", needs_staff=False, emergency=False)

    async def assess_safety(self, text, recent):
        self.seen.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError("model unavailable")
        return self.level


def test_model_safety_check_raises_none_to_crisis(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        model = SafetyModel(Level.CRISIS)
        session.call.interpreter = model
        reply = await session.say("Honestly I just feel like everyone would be better off")
        assert (reply.text, reply.outcome) == (CRISIS_SCRIPT, "emergency")
        assert model.seen == ["Honestly I just feel like everyone would be better off"]
        assert routed(session, "emergency_routed")["category"] == MODEL_CATEGORY
        (item,) = await work_items(clinic, session)
        assert item["reason"] == "crisis"

    run(scenario())


def test_model_safety_check_cannot_lower_a_rule_based_level(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        session.call.interpreter = SafetyModel(Level.NONE)
        reply = await session.say("I can't breathe")
        assert (reply.text, reply.outcome) == (EMERGENCY_SCRIPT, "emergency")
        urgent = clinic.session()
        urgent.call.interpreter = SafetyModel(Level.NONE)
        reply = await urgent.say("I fell and can't put weight on my leg")
        assert (reply.text, reply.outcome) == (URGENT_SCRIPT, "handoff")

    run(scenario())
    assert raise_level(Triage(Level.EMERGENCY, "breathing"), Level.NONE).level is Level.EMERGENCY
    assert raise_level(Triage(Level.POISONING, "poisoning"), Level.CRISIS).level is Level.POISONING
    assert raise_level(Triage(Level.NONE), Level.URGENT).level is Level.URGENT


@pytest.mark.parametrize(
    "model",
    [SafetyModel(Level.CRISIS, delay=60), SafetyModel(error=True)],
    ids=["timeout", "error"],
)
def test_model_safety_failure_falls_back_to_rules(tmp_path, monkeypatch, model):
    real_timeout = asyncio.timeout
    # Shrink the 8 second limit so the timeout case runs quickly.
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: real_timeout(min(seconds, 0.05)))

    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        session.call.interpreter = model
        reply = await session.say("I fell and can't put weight on my leg")
        assert (reply.text, reply.outcome) == (URGENT_SCRIPT, "handoff")
        routine = clinic.session()
        routine.call.interpreter = model
        assert (await routine.say("book a follow-up")).outcome is None

    run(scenario())


def test_interpreters_without_a_safety_check_keep_working(tmp_path):
    class Plain:
        async def interpret_turn(self, context, actions):
            return TurnMeaning(action="book", value="", needs_staff=False, emergency=False)

    async def scenario():
        session = Clinic(tmp_path).session()
        session.call.interpreter = Plain()
        assert "for you or for someone else" in (await session.say("book a follow-up")).text

    run(scenario())


def test_model_safety_answers_are_validated_strictly():
    assert model_level("CRISIS") is Level.CRISIS
    assert model_level(" urgent ") is Level.URGENT
    for value in ("AMBIGUOUS", "crisis!", "", None, 3, "EMERGENCY because"):
        assert model_level(value) is None


def test_urgent_work_sorts_before_other_open_work(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        urgent = clinic.session()
        await urgent.say("I fell and can't put weight on my leg")
        await clinic.ops.finish(urgent.trace)
        staff = clinic.session()
        await staff.say("I want to talk to a person")
        # The staff request is newer, but the urgent work still comes first.
        tasks = await work_items(clinic, staff)
        assert [item["reason"] for item in tasks] == ["urgent_symptoms", "staff_requested"]
        assert [item["urgent"] for item in tasks] == [True, False]

    run(scenario())
