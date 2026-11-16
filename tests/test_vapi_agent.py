"""The Vapi line with SWITCHBOARD_CALL_ENGINE=agent: Claude (a scripted fake here) runs the call."""

import asyncio

import anthropic
import pytest
from fastapi.testclient import TestClient
from test_agent import MARIA, FakeClient, text, tool_results, tools
from test_vapi import CALLER, SECRET, PhoneCall

from switchboard.agent import AgentCall
from switchboard.api import app
from switchboard.safety import EMERGENCY_SCRIPT
from switchboard.spoken import say_numbers
from switchboard.store import DemoStore

OPS = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture
def line(tmp_path, monkeypatch):
    """The Vapi app with the agent engine; ``line.model(script)`` installs the fake model."""
    database = tmp_path / "vapi.sqlite"
    monkeypatch.setenv("SWITCHBOARD_DB", str(database))
    monkeypatch.setenv("SWITCHBOARD_VAPI_SECRET", SECRET)
    monkeypatch.setenv("SWITCHBOARD_VAPI_CALLERS", CALLER)
    monkeypatch.setenv("SWITCHBOARD_OPERATIONS_TOKEN", SECRET)
    monkeypatch.setenv("SWITCHBOARD_STAFF_TRANSCRIPTS", "1")
    monkeypatch.setenv("SWITCHBOARD_CALL_ENGINE", "agent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.delenv("SWITCHBOARD_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_SMTP_HOST", raising=False)
    with TestClient(app) as client:
        assert app.state.call_engine == "agent"
        # One shared client is built at startup; tests swap in the fake before any call.
        assert isinstance(app.state.agent_client, anthropic.AsyncAnthropic)

        class Line:
            def __init__(self):
                self.client, self.store = client, DemoStore(database)

            def model(self, script: list) -> FakeClient:
                app.state.agent_client = fake = FakeClient(script)
                return fake

            def operations(self) -> dict:
                return client.get("/api/operations", headers=OPS).json()

        yield Line()


def agent_of(call: PhoneCall) -> AgentCall:
    session = app.state.vapi.sessions[call.metadata["id"]]
    assert isinstance(session.call, AgentCall)
    return session.call


def caller_words(fake: FakeClient, request: int = -1) -> str:
    """The caller text in the latest user message the model received."""
    message = fake.messages.requests[request]["messages"][-1]
    assert message["role"] == "user"
    return " ".join(block["text"] for block in message["content"] if block["type"] == "text")


def held(store: DemoStore) -> list[dict]:
    return asyncio.run(store.search("Slot", status="busy-tentative"))


OFFER = [
    tools(("verify_patient", MARIA), ("find_times", {"visit_type": "follow-up"})),
    text("I have a few times. Which one works?"),
    tools(("propose_booking", {"option": 1})),
    text("Shall I book it?"),
]


def offer(call: PhoneCall) -> str:
    call.say("Hi, I'm Maria Lopez, born July 2nd 1984. I need a follow-up.")
    proposal = call.say("The first one please.")
    assert proposal.startswith("Book your follow-up on ")
    return proposal


def test_agent_books_through_vapi_with_keypad_birth_date(line):
    fake = line.model(
        [
            text("Sure. What's your full name and date of birth?"),
            text("Thanks. Please key in your date of birth."),
            *OFFER,
            tools(("confirm_change", {})),
            text("You're booked. Anything else?"),
        ]
    )
    call = PhoneCall(line.client)
    assert "date of birth" in call.say("I'd like to book a follow-up.")
    call.say("Maria Lopez")
    # Keypad digits reach the agent as text it can read, not through a keypad stage.
    assert "Which one" in call.say("User's Keypad Entry: 19840702")
    assert caller_words(fake, 2) == "User keyed: 19840702"
    agent = agent_of(call)
    assert tool_results(fake, 3)["verify_patient"]["verified"] is True
    # The staff transcript shows that digits were keyed, not the birth date itself.
    assert {"speaker": "caller", "text": "[Digits entered on keypad]"} in agent.trace.transcript
    proposal = call.say("The first one please.")
    assert proposal == say_numbers(agent.read_back) and proposal.startswith(
        "Book your follow-up on "
    )
    (slot,) = held(line.store)
    call.heard(proposal)
    assert "booked" in call.say("Yes, go ahead.").lower()
    assert tool_results(fake)["confirm_change"]["result"] == "booked"
    (appointment,) = asyncio.run(line.store.search("Appointment"))
    assert appointment["status"] == "booked"
    assert appointment["slot"][0]["reference"] == f"Slot/{slot['id']}"
    assert appointment["participant"][0]["actor"]["reference"] == "Patient/maria-demo"
    assert not held(line.store)
    call.event("end-of-call-report")
    assert "ended" in call.say("Hello?").lower()
    assert app.state.sessions == 0
    data = line.operations()
    assert any(
        item["transport"] == "vapi" and item["outcome"] == "booked" for item in data["recent"]
    )
    (notice,) = data["notices"]
    assert notice["status"] == "captured"


def test_agent_answers_two_caller_messages_as_one_turn(line):
    fake = line.model([text("Thanks Maria. What kind of visit do you need?")])
    call = PhoneCall(line.client)
    call.messages.append({"role": "user", "content": "Hi, this is Maria Lopez,"})
    reply = call.say("born July 2nd 1984.")
    assert reply == "Thanks Maria. What kind of visit do you need?"
    # One model request, carrying both messages joined as one caller turn.
    assert len(fake.messages.requests) == 1
    assert caller_words(fake) == "Hi, this is Maria Lopez, born July 2nd 1984."


def test_agent_interrupted_proposal_is_read_again_before_any_write(line):
    fake = line.model(
        [
            *OFFER,
            tools(("confirm_change", {})),
            text("Done, you're booked."),
            tools(("confirm_change", {})),
            text("You're booked. Anything else?"),
        ]
    )
    call = PhoneCall(line.client)
    proposal = offer(call)
    call.heard(proposal, interrupted=True)
    again = call.say("Yes.")
    assert "didn't hear" in tool_results(fake)["confirm_change"]["error"]
    # The model's claim is replaced by the read-back the caller must hear in full.
    assert again == proposal
    assert asyncio.run(line.store.search("Appointment")) == []
    assert held(line.store)
    call.heard(again)
    assert "booked" in call.say("Yes.").lower()
    (appointment,) = asyncio.run(line.store.search("Appointment"))
    assert appointment["status"] == "booked"


def test_agent_emergency_gets_911_without_the_model_and_releases_the_hold(line):
    fake = line.model(list(OFFER))
    call = PhoneCall(line.client)
    offer(call)
    (slot,) = held(line.store)
    requests = len(fake.messages.requests)
    reply = call.say("I have crushing chest pain right now")
    assert reply == say_numbers(EMERGENCY_SCRIPT) and "nine one one" in reply
    assert len(fake.messages.requests) == requests
    assert asyncio.run(line.store.read("Slot", slot["id"]))["status"] == "free"
    assert asyncio.run(line.store.search("Appointment")) == []
    # The call ended on the emergency; Vapi's next request gets the closing line.
    assert "ended" in call.say("Hello?").lower()
    assert app.state.sessions == 0
    data = line.operations()
    assert any(
        item["transport"] == "vapi" and item["outcome"] == "emergency" for item in data["recent"]
    )
    assert any(task["reason"] == "emergency" for task in data["tasks"])


def test_agent_hangup_releases_the_held_slot(line):
    line.model(list(OFFER))
    call = PhoneCall(line.client)
    offer(call)
    assert held(line.store)
    call.event("end-of-call-report")
    assert not held(line.store)
    assert asyncio.run(line.store.search("Appointment")) == []
    assert app.state.sessions == 0
    assert call.metadata["id"] not in app.state.vapi.sessions


def test_agent_handoff_creates_a_staff_work_item(line):
    line.model(
        [
            tools(("transfer_to_staff", {"reason": "billing question"})),
            text("I'll pass you to the front desk."),
        ]
    )
    call = PhoneCall(line.client)
    assert "front desk" in call.say("I have a question about my bill.")
    data = line.operations()
    assert any(
        item["transport"] == "vapi" and item["outcome"] == "handoff" for item in data["recent"]
    )
    assert data["tasks"]


def test_scripted_call_when_engine_is_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "vapi.sqlite"))
    monkeypatch.setenv("SWITCHBOARD_VAPI_SECRET", SECRET)
    monkeypatch.setenv("SWITCHBOARD_VAPI_CALLERS", CALLER)
    monkeypatch.delenv("SWITCHBOARD_CALL_ENGINE", raising=False)
    with TestClient(app):
        assert app.state.call_engine == "scripted" and app.state.agent_client is None
    monkeypatch.setenv("SWITCHBOARD_CALL_ENGINE", "agent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:
        # Without a key the line stays on the scripted call.
        assert app.state.call_engine == "scripted"
        call = PhoneCall(client)
        call.say("Book a follow-up")
        session = app.state.vapi.sessions[call.metadata["id"]]
        assert not isinstance(session.call, AgentCall)


def test_agent_knows_the_greeting_vapi_spoke(line):
    fake = line.model([text("Sure. Who is the appointment for?")])
    call = PhoneCall(line.client)
    call.messages[0]["content"] = "Hi, thanks for calling. Please use fictional details."
    call.say("I'd like to book a visit.")
    assert "Please use fictional details." in fake.messages.requests[0]["system"]
