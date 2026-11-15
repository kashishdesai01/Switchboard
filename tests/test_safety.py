import asyncio
from datetime import datetime

import pytest

from switchboard.config import BAYVIEW
from switchboard.conversation import Call
from switchboard.intents import TurnMeaning
from switchboard.safety import SafetySignal, safety_signal
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
