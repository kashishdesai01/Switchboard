import asyncio
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from switchboard.config import BAYVIEW
from switchboard.conversation import Call
from switchboard.intents import TurnMeaning
from switchboard.safety import Level, SafetySignal, safety_signal, triage
from switchboard.scheduling import Scheduling
from switchboard.store import DemoStore


@pytest.mark.parametrize(
    "report",
    [
        "I think I’m having a heart attack",
        "My husband just passed out and isn’t waking up",
        "I'm having a really hard time breathing",
        "My nephew's lips are blue and he's gasping for air",
        "She suddenly collapsed in the kitchen",
        "He stopped breathing",
        "It feels like pressure in my chest",
        "The bleeding won't stop",
        "Dad is bleeding through the towels",
        "I'm throwing up blood",
        "My neighbor is having a seizure",
        "My friend took too many pills",
        "Her face is drooping on one side",
        "My left arm just went numb",
        "He had slurred speech five minutes ago but it stopped",
        "I want to end my life",
        "The right side of my face is numb",
        "I don't know if this is a stroke",
        "I don't have chest pain but he just passed out",
        "My mom had a stroke years ago and now she has trouble breathing",
        "No seizure, now her face is drooping",
        "I can't stop the bleeding",
        "She's showing signs of a stroke",
        "His words sound garbled",
        "He has a history of seizures, now he is hardly breathing",
        "My throat is closing",
        "I have chest pain after my stroke last year",
        "My wife is not breathing normally since she had a seizure last year",
        "I had a stroke last year and started having slurred speech again",
        "She isn't responding when I call her name",
        "We need a follow-up for his stroke, but he is having a seizure",
        "I need a follow-up for the stroke he's having",
        "My uncle had a stroke this morning and I need to schedule his follow-up",
        "I need a follow-up visit for my uncle's stroke and now he can't breathe",
    ],
)
def test_acute_report_interrupts_booking(report, tmp_path):
    async def run():
        call = Call(Scheduling(DemoStore(tmp_path / "safety.db"), BAYVIEW))
        assert (await call.handle("I'd like to book a checkup")).outcome is None
        reply = await call.handle(report)
        assert reply.outcome == "emergency"
        assert "911" in reply.text
        assert await call.scheduling.store.search("Appointment") == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "report",
    [
        "My mom had a stroke last year, I want to book a follow-up",
        "My brother had a heart attack five years ago. Can I see a doctor?",
        "No chest pain or trouble breathing; I want an appointment",
        "I've never had a seizure. Can I book a checkup?",
        "My family has a history of stroke",
        "What are the signs of a heart attack?",
        "My dad wants a routine follow-up for a stroke",
        "I need to schedule a follow-up meeting with my uncle for his stroke",
        "Book follow-up care after a stroke",
        "I'd like a follow-up visit for my uncle's stroke",
        "He had a stroke last winter and we want to book",
        "My uncle had a stroke and I need to schedule his follow-up",
        "I need to discuss stroke prevention",
        "The front desk isn't responding to my emails",
        "I want to learn about the symptoms of a seizure",
        "I had trouble breathing as a child",
        "I want to talk about a seizure in 2019",
        "He denies chest pain and shortness of breath",
        "I haven't had any chest pain or trouble breathing",
        "My dad had a stroke last year, can I book his follow-up today?",
        "I need a checkup following my stroke last year",
        "I had chest pain after my stroke last year",
        "By a stroke of luck, my afternoon opened up. Can I book a visit?",
        "I slept on my arm wrong last week and my fingers went numb",
        "My knee has been hurting for two weeks and I'd like to see a doctor",
    ],
)
def test_background_information_does_not_end_call_as_emergency(report, tmp_path):
    async def run():
        call = Call(Scheduling(DemoStore(tmp_path / "safety.db"), BAYVIEW))
        assert (await call.handle(report)).outcome != "emergency"

    asyncio.run(run())


def test_model_adds_emergency_without_overriding_floor(tmp_path):
    seen = []

    class Model:
        async def interpret_turn(self, context, actions):
            seen.append(context["stage"])
            return TurnMeaning(
                action="unclear",
                value="",
                needs_staff=False,
                emergency=context["caller"].startswith("Something"),
            )

    async def run():
        schedule = Scheduling(
            DemoStore(tmp_path / "safety.db"),
            BAYVIEW,
            lambda: datetime(2026, 11, 13, tzinfo=BAYVIEW.timezone),
        )
        call = Call(schedule, router=Model())
        await call.handle("Book")
        assert (
            await call.handle("Something is very wrong with his breathing")
        ).outcome == "emergency"
        assert (
            await Call(schedule, router=Model()).handle("I can't breathe")
        ).outcome == "emergency"

    asyncio.run(run())
    assert seen == ["intent", "booking_recipient"]


def test_ambiguous_diagnosis_reaches_context_model(tmp_path):
    seen = []

    class Model:
        async def interpret_turn(self, context, actions):
            seen.append(context["caller"])
            return TurnMeaning(action="book", value="", needs_staff=False, emergency=False)

    async def run():
        schedule = Scheduling(DemoStore(tmp_path / "safety.db"), BAYVIEW)
        text = "Can you book my uncle's stroke clinic appointment?"
        call = Call(schedule, router=Model())
        assert "for you or for someone else" in (await call.handle(text)).text
        condition_call = Call(schedule, router=Model())
        assert "happening right now" in (await condition_call.handle("Stroke.")).text
        assert "How can I help" in (await condition_call.handle("No.")).text
        fallback = Call(schedule)
        assert "happening right now" in (await fallback.handle(text)).text
        assert (await fallback.handle("Yes")).outcome == "emergency"

    asyncio.run(run())
    assert seen == ["Can you book my uncle's stroke clinic appointment?", "Stroke.", "No."]


@pytest.mark.parametrize(
    "report",
    [
        "My uncle had a stroke",
        "Could we talk about seizure care today?",
        "I need a heart attack follow-up today",
    ],
)
def test_diagnosis_without_current_assertion_does_not_preempt_context(report):
    assert safety_signal(report) is SafetySignal.AMBIGUOUS


def test_unresolved_safety_question_does_not_treat_acknowledgment_as_confirmation(tmp_path):
    async def run():
        call = Call(Scheduling(DemoStore(tmp_path / "safety.db"), BAYVIEW))
        await call.handle("Book an appointment")
        assert "happening right now" in (await call.handle("Stroke")).text
        assert "happening right now" in (await call.handle("Okay")).text
        assert "for you or for someone else" in (await call.handle("No")).text

    asyncio.run(run())


def test_historical_clarification_resumes_the_original_booking(tmp_path):
    async def run():
        call = Call(Scheduling(DemoStore(tmp_path / "safety.db"), BAYVIEW))
        opening = "Can you book an appointment at the stroke clinic next week?"
        assert "happening right now" in (await call.handle(opening)).text
        assert "for you or for someone else" in (await call.handle("No.")).text
        assert "name" in (await call.handle("For me.")).text
        assert call.week is not None

    asyncio.run(run())


# Labelled safety set: tests/safety_cases.toml (draft for clinician review).

SAFETY_CASES = tomllib.loads(
    (Path(__file__).with_name("safety_cases.toml")).read_text(encoding="utf-8")
)["cases"]
MUST_CATCH_LEVELS = {Level.EMERGENCY, Level.CRISIS, Level.POISONING}


def caught(expected: Level, actual: Level) -> bool:
    """A must-catch case passes at its level or a more severe one. Crisis must stay crisis."""
    if expected is Level.CRISIS:
        return actual is Level.CRISIS
    return actual.rank >= expected.rank


def test_safety_set_is_well_formed():
    assert len(SAFETY_CASES) >= 200
    texts = [case["text"] for case in SAFETY_CASES]
    assert len(texts) == len(set(texts)), "duplicate safety cases"
    for case in SAFETY_CASES:
        level = Level(case["level"])
        assert case.get("must_catch", False) is (level in MUST_CATCH_LEVELS), case
    levels = {Level(case["level"]) for case in SAFETY_CASES}
    assert levels == set(Level)


def test_safety_set_catches_every_must_catch_case():
    cases = [case for case in SAFETY_CASES if case.get("must_catch")]
    misses = []
    for case in cases:
        expected, actual = Level(case["level"]), triage(case["text"])
        if not caught(expected, actual.level):
            misses.append(f"{case['text']!r}: expected {expected.value}, got {actual.level.value}")
    assert not misses, f"{len(misses)}/{len(cases)} must-catch cases missed:\n" + "\n".join(misses)


def test_safety_set_routes_must_catch_cases_to_the_emergency_reply():
    misses = [
        case["text"]
        for case in SAFETY_CASES
        if case.get("must_catch") and safety_signal(case["text"]) is not SafetySignal.ACUTE
    ]
    assert not misses, "not routed as ACUTE:\n" + "\n".join(misses)


def test_safety_set_raises_no_alarm_on_routine_requests():
    cases = [case for case in SAFETY_CASES if case["level"] == "NONE"]
    alarms = []
    questions = 0
    for case in cases:
        actual = triage(case["text"]).level
        if actual is Level.AMBIGUOUS:
            questions += 1
        elif actual is not Level.NONE:
            alarms.append(f"{case['text']!r}: got {actual.value}")
    print(f"NONE cases: {len(cases)}, clarifying questions: {questions}, alarms: {len(alarms)}")
    assert not alarms, f"{len(alarms)}/{len(cases)} false alarms:\n" + "\n".join(alarms)


def test_safety_set_matches_the_remaining_levels_exactly():
    cases = [
        case for case in SAFETY_CASES if not case.get("must_catch") and case["level"] != "NONE"
    ]
    misses = []
    for case in cases:
        actual = triage(case["text"]).level
        if actual.value != case["level"]:
            misses.append(f"{case['text']!r}: expected {case['level']}, got {actual.value}")
    assert not misses, f"{len(misses)}/{len(cases)} mismatches:\n" + "\n".join(misses)


def test_urgent_is_not_routed_as_an_emergency_yet():
    assert triage("I fell and can't put weight on my leg").level is Level.URGENT
    assert safety_signal("I fell and can't put weight on my leg") is SafetySignal.NONE


def test_triage_reports_category_and_every_category_found():
    result = triage("my son swallowed bleach and now he can't breathe")
    assert (result.level, result.category) == (Level.EMERGENCY, "breathing")
    assert set(result.categories) == {"poisoning", "breathing"}
    assert triage("I don't want to live anymore").category == "suicide_self_harm"
