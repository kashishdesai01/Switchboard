"""AgentCall with a scripted fake Anthropic client: the guarantees hold in tool code."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_gaps import Clinic, run

from switchboard.agent import AgentCall
from switchboard.agent.engine import APOLOGY, MAX_TOOL_ROUNDS, UNAVAILABLE
from switchboard.call.vocabulary import Reply
from switchboard.safety import EMERGENCY_SCRIPT
from switchboard.scheduling import Scheduling
from switchboard.spoken import say_numbers
from switchboard.store import DemoStore

MARIA = {
    "full_name": "Maria Lopez",
    "birth_date": "1984-07-02",
    "for_self": True,
    "permission_stated": False,
    "request": "book",
}


def text(words: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=words)], stop_reason="end_turn"
    )


def tools(*uses: tuple[str, dict]) -> SimpleNamespace:
    blocks = [
        SimpleNamespace(type="tool_use", id=f"toolu_{name}_{index}", name=name, input=args)
        for index, (name, args) in enumerate(uses)
    ]
    return SimpleNamespace(content=blocks, stop_reason="tool_use")


class FakeMessages:
    """Returns the scripted responses in order; an exception in the script is raised."""

    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []

    async def create(self, **params):
        self.requests.append(json.loads(json.dumps(params)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FakeClient:
    def __init__(self, script: list):
        self.messages = FakeMessages(script)


class Session:
    def __init__(self, call: AgentCall):
        self.call = call
        self.reply: Reply = call.greeting()

    async def say(self, words: str, *, heard: bool = True) -> Reply:
        if heard:
            self.call.playback_finished(self.reply.id)
        self.reply = await self.call.handle(words)
        return self.reply


def agent(clinic: Clinic, script: list, **options) -> tuple[Session, FakeClient]:
    client = FakeClient(script)
    trace = clinic.ops.start("test")
    settings = {"allow_registration": True, "frontdesk": clinic.frontdesk, **options}
    call = AgentCall(clinic.scheduling, client, trace, model="test-model", **settings)
    return Session(call), client


def tool_results(client: FakeClient, request: int = -1) -> dict[str, dict]:
    """Tool results the model received in a request, by tool name."""
    messages = client.messages.requests[request]["messages"]
    names = {
        block["id"]: block["name"]
        for message in messages
        if message["role"] == "assistant"
        for block in message["content"]
        if block["type"] == "tool_use"
    }
    return {
        names[block["tool_use_id"]]: json.loads(block["content"])
        for block in messages[-1]["content"]
        if block["type"] == "tool_result"
    }


async def slot_status(clinic: Clinic, slot_id: str) -> str:
    return (await clinic.store.read("Slot", slot_id))["status"]


def test_successful_booking_writes_exactly_the_proposed_slot(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, client = agent(
            clinic,
            [
                tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
                text("I have two times. Which works?"),
                tools(("propose_booking", {"option": 2})),
                text("Great."),
                tools(("confirm_change", {})),
                text("You're all set. Anything else?"),
            ],
        )
        reply = await session.say("Hi, I'm Maria Lopez, born July 2nd 1984. I need a follow-up.")
        assert tool_results(client)["verify_patient"]["verified"] is True
        offered = list(session.call.options)
        assert len(offered) >= 2
        reply = await session.say("The second one please.")
        # The proposal is always read back word for word and marked for confirmation.
        assert reply.confirmation and reply.text.startswith("Book your follow-up on ")
        assert reply.text == say_numbers(session.call.read_back)
        assert await slot_status(clinic, offered[1]["id"]) == "busy-tentative"
        reply = await session.say("Yes.")
        assert tool_results(client)["confirm_change"]["result"] == "booked"
        assert reply.outcome == "booked" and not reply.ends_call
        assert session.call.summary_outcome() == "booked"
        (appointment,) = await clinic.booked()
        assert appointment["slot"][0]["reference"] == f"Slot/{offered[1]['id']}"
        assert appointment["participant"][0]["actor"]["reference"] == "Patient/maria-demo"
        events = [event["event"] for event in session.call.trace.events]
        assert "mutation_dispatched" in events and "mutation_committed" in events
        assert session.call.trace.outcomes == ["booked"]
        with sqlite3.connect(clinic.ops.path) as db:
            rows = db.execute("SELECT action, status FROM mutations").fetchall()
        assert rows == [("book", "committed")]
        assert session.call.stage == "anything_else"

    run(scenario())


def test_confirm_refused_in_same_turn_as_proposal(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, client = agent(
            clinic,
            [
                tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
                tools(("propose_booking", {"option": 1}), ("confirm_change", {})),
                text("You're booked!"),
            ],
        )
        reply = await session.say("Maria Lopez, 1984-07-02, book me the first time you have.")
        result = tool_results(client)["confirm_change"]
        assert result["ok"] is False and "hasn't answered" in result["error"]
        assert await clinic.booked() == []
        # The model's claim is replaced by the read-back the caller must answer.
        assert reply.outcome is None and reply.confirmation
        assert reply.text == say_numbers(session.call.read_back)

    run(scenario())


def test_confirm_refused_when_read_back_was_interrupted(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, client = agent(
            clinic,
            [
                tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
                tools(("propose_booking", {"option": 1})),
                text("Shall I book it?"),
                tools(("confirm_change", {})),
                text("Done."),
            ],
        )
        await session.say("Maria Lopez, July 2 1984, a follow-up please, the first time.")
        session.call.interrupt()
        reply = await session.say("Yes.", heard=False)
        assert "didn't hear" in tool_results(client)["confirm_change"]["error"]
        assert await clinic.booked() == []
        assert reply.confirmation and reply.text == say_numbers(session.call.read_back)

    run(scenario())


def test_unverified_patient_reads_and_writes_nothing(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session, client = agent(
            clinic,
            [
                tools(
                    ("list_appointments", {}),
                    ("find_times", {"visit_type": "follow-up"}),
                    ("propose_cancel", {"appointment": 1}),
                    ("confirm_change", {}),
                ),
                text("Could I have your full name and date of birth?"),
            ],
        )
        await session.say("Cancel my appointment.")
        results = tool_results(client)
        assert all(result["ok"] is False for result in results.values())
        assert "not verified" in results["list_appointments"]["error"]
        assert "appointments" not in results["list_appointments"]
        assert "not verified" in results["find_times"]["error"]
        assert results["confirm_change"]["error"].startswith("Nothing is proposed")
        assert len(await clinic.booked()) == 1

    run(scenario())


def test_birth_dates_must_be_full(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        partial = [
            {**MARIA, "birth_date": date} for date in ("1984-07", "July 2 1984", "1984-02-30")
        ]
        session, client = agent(
            clinic,
            [tools(*(("verify_patient", args) for args in partial)), text("What year?")],
        )
        await session.say("Maria Lopez, July 2nd.")
        messages = client.messages.requests[-1]["messages"][-1]["content"]
        assert all(json.loads(block["content"])["ok"] is False for block in messages)
        assert session.call.patient is None

    run(scenario())


def test_proxy_needs_stated_permission(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        uncle = {**MARIA, "for_self": False}
        session, client = agent(
            clinic,
            [
                tools(("verify_patient", uncle)),
                text("Do you have her permission?"),
                tools(("verify_patient", {**uncle, "permission_stated": True})),
                text("Thanks, I found the record."),
            ],
        )
        await session.say("Book for my aunt Maria Lopez, July 2 1984.")
        assert "permission" in tool_results(client)["verify_patient"]["error"]
        assert session.call.patient is None
        await session.say("Yes I do.")
        assert tool_results(client)["verify_patient"]["patient"] == "other"
        assert session.call.party.proxy and session.call.patient == "maria-demo"

    run(scenario())


def test_proxy_booking_for_unknown_patient_registers_them_with_a_read_back(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        uncle = {
            **MARIA,
            "full_name": "Carl Smith",
            "birth_date": "1955-08-18",
            "for_self": False,
            "permission_stated": True,
        }
        session, client = agent(
            clinic,
            [
                tools(("verify_patient", uncle), ("find_times", {"visit_type": "first visit"})),
                text("I'll set him up as a new patient. Which time works?"),
                tools(("propose_booking", {"option": 1})),
                text("Shall I book it?"),
            ],
        )
        await session.say("Book for my uncle Carl Smith, August 18 1955. I have his permission.")
        assert tool_results(client)["verify_patient"]["new_patient"] is True
        reply = await session.say("The first one.")
        assert "Carl Smith, born August eighteenth, nineteen fifty five" in reply.text
        assert "the patient's first visit" in reply.text

    run(scenario())


class OtherStore:
    """Delegates to the demo store but is not one, like a FHIR backend."""

    def __init__(self, store: DemoStore):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_proxy_refused_outside_the_demo_store(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        uncle = {**MARIA, "for_self": False, "permission_stated": True}
        for options in (
            {"frontdesk": None},
            {
                "scheduling": Scheduling(
                    OtherStore(clinic.store), clinic.scheduling.clinic, clinic.scheduling.now
                )
            },
        ):
            client = FakeClient(
                [
                    tools(("verify_patient", uncle)),
                    tools(("transfer_to_staff", {"reason": "proxy request"})),
                    text("The front desk will help with that."),
                ]
            )
            settings = {"frontdesk": clinic.frontdesk, "scheduling": clinic.scheduling, **options}
            scheduling = settings.pop("scheduling")
            call = AgentCall(scheduling, client, clinic.ops.start("test"), **settings)
            session = Session(call)
            first = await session.say("I'm booking for my uncle, I have his permission.")
            assert first.outcome == "handoff" and first.ends_call
            request = client.messages.requests[1]
            verify = json.loads(request["messages"][-1]["content"][0]["content"])
            assert verify["ok"] is False and "someone else" in verify["error"]
            assert call.patient is None
            if call.frontdesk:
                assert call.trace.work[1]["topic"] == "proxy request"

    run(scenario())


def test_emergency_never_reaches_the_model(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, client = agent(
            clinic,
            [
                tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
                tools(("propose_booking", {"option": 1})),
                text("Shall I?"),
            ],
        )
        await session.say("Maria Lopez, July 2 1984, follow-up.")
        held = session.call.change.hold.slot["id"]
        calls = len(client.messages.requests)
        reply = await session.say("I have crushing chest pain right now")
        assert len(client.messages.requests) == calls
        assert reply.text == say_numbers(EMERGENCY_SCRIPT)
        assert reply.outcome == "emergency" and reply.ends_call
        assert session.call.trace.work[0] == "emergency"
        assert await slot_status(clinic, held) == "free"
        reply = await session.say("hello?")
        assert reply.outcome == "closed" and len(client.messages.requests) == calls

    run(scenario())


def test_hangup_releases_held_slot(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, _ = agent(
            clinic,
            [
                tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
                tools(("propose_booking", {"option": 1})),
                text("Book it?"),
            ],
        )
        await session.say("Maria Lopez, July 2 1984, the first follow-up.")
        held = session.call.change.hold.slot["id"]
        assert await slot_status(clinic, held) == "busy-tentative"
        await session.call.close()
        assert await slot_status(clinic, held) == "free"
        assert await clinic.booked() == []

    run(scenario())


def test_decline_releases_hold_and_writes_nothing(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, _ = agent(
            clinic,
            [
                tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
                tools(("propose_booking", {"option": 1})),
                text("Book it?"),
                tools(("decline_change", {})),
                text("No problem, I made no changes. Anything else?"),
            ],
        )
        await session.say("Maria Lopez, July 2 1984, the first follow-up.")
        held = session.call.change.hold.slot["id"]
        reply = await session.say("No, don't.")
        assert reply.outcome == "declined"
        assert await slot_status(clinic, held) == "free"
        assert await clinic.booked() == []

    run(scenario())


def test_cancel_existing_appointment(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session, _ = agent(
            clinic,
            [
                tools(
                    ("verify_patient", {**MARIA, "request": "cancel"}), ("list_appointments", {})
                ),
                tools(("propose_cancel", {"appointment": 1})),
                text("Cancel it?"),
                tools(("confirm_change", {})),
                text("Your appointment was cancelled. Anything else?"),
            ],
        )
        reply = await session.say("Cancel my appointment, Maria Lopez, July 2 1984.")
        assert reply.text.startswith("Cancel your appointment on ")
        reply = await session.say("Yes.")
        assert reply.outcome == "cancelled"
        assert await clinic.booked() == []

    run(scenario())


def test_model_failure_apologizes_then_hands_off(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, client = agent(clinic, [TimeoutError(), ConnectionError()])
        reply = await session.say("I'd like to book.")
        assert reply.text == APOLOGY and reply.outcome is None
        reply = await session.say("I said I'd like to book.")
        assert reply.text == UNAVAILABLE
        assert reply.outcome == "handoff" and reply.ends_call
        assert session.call.trace.work[0] == "staff_requested"
        # The retry carried both caller turns.
        texts = [block["text"] for block in client.messages.requests[-1]["messages"][-1]["content"]]
        assert texts == ["I'd like to book.", "I said I'd like to book."]

    run(scenario())


def test_one_failure_then_recovery_resets(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, _ = agent(
            clinic, [TimeoutError(), text("Sure, who is it for?"), TimeoutError(), text("Okay.")]
        )
        assert (await session.say("Book please.")).text == APOLOGY
        assert (await session.say("Book please.")).text == "Sure, who is it for?"
        assert (await session.say("Me.")).text == APOLOGY
        assert (await session.say("Me.")).outcome is None

    run(scenario())


def test_tool_rounds_are_capped(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        loop = [tools(("list_appointments", {})) for _ in range(MAX_TOOL_ROUNDS)]
        session, client = agent(clinic, [*loop, text("Who am I speaking with?")])
        reply = await session.say("Hi")
        assert reply.text == "Who am I speaking with?"
        requests = client.messages.requests
        assert len(requests) == MAX_TOOL_ROUNDS + 1
        assert requests[-1]["tool_choice"] == {"type": "none"}
        assert "tool_choice" not in requests[0]

    run(scenario())


def test_system_prompt_describes_clinic(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session, client = agent(clinic, [text("Our hours are on weekdays.")])
        await session.say("What are your hours?")
        system = client.messages.requests[0]["system"]
        bayview = clinic.scheduling.clinic
        assert bayview.name in system and bayview.weekday_hours in system
        assert all(name in system for name in bayview.providers.values())
        assert all(payer in system for payer in bayview.accepted_payers)
        names = {tool["name"] for tool in client.messages.requests[0]["tools"]}
        assert {"verify_patient", "find_times", "confirm_change", "end_call"} <= names

    run(scenario())


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("at 9:30 AM", "at nine thirty A M"),
        ("at 10:05 PM", "at ten oh five P M"),
        ("at 12:00 PM", "at noon"),
        ("on Monday, November 16", "on Monday, November sixteenth"),
        ("born the 18th of August, 1955", "born the eighteenth of August, nineteen fifty five"),
        ("born March 3, 2005", "born March third, two thousand five"),
        ("call 911", "call nine one one"),
        ("option 2", "option two"),
    ],
)
def test_spoken_replies_say_numbers_as_words(written, spoken):
    assert say_numbers(written) == spoken


def test_unclear_answer_to_read_back_keeps_proposal_open_for_the_next_yes(tmp_path):
    """Live call: "The first one." after the read-back, then "Yes." must book."""

    async def scenario():
        clinic = Clinic(tmp_path)
        session, client = agent(
            clinic,
            [
                tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
                text("I have two times. Which works?"),
                tools(("propose_booking", {"option": 1})),
                text("Shall I?"),
                text("Just to be sure, should I book that time?"),  # paraphrase, no read-back
                tools(("confirm_change", {})),
                text("You're all set."),
            ],
        )
        await session.say("Hi, I'm Maria Lopez, born July 2nd 1984. I need a follow-up.")
        await session.say("Whatever is soonest.")
        reply = await session.say("The first one.")
        assert reply.confirmation
        assert reply.text.endswith(say_numbers(session.call.read_back))
        reply = await session.say("Yes.")
        assert reply.outcome == "booked"
        assert len(await clinic.booked()) == 1

    run(scenario())
