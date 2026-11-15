import asyncio
import json
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from switchboard.api import app
from switchboard.store import DemoStore

SECRET = "vapi_synthetic_test_secret_only_123456"
CALLER = "+12025550123"
HEADERS = {"X-Switchboard-Vapi-Secret": SECRET}


@pytest.fixture
def phone(tmp_path, monkeypatch):
    database = tmp_path / "vapi.sqlite"
    monkeypatch.setenv("SWITCHBOARD_DB", str(database))
    monkeypatch.setenv("SWITCHBOARD_VAPI_SECRET", SECRET)
    monkeypatch.setenv("SWITCHBOARD_VAPI_CALLERS", CALLER)
    monkeypatch.setenv("SWITCHBOARD_OPERATIONS_TOKEN", SECRET)
    monkeypatch.delenv("SWITCHBOARD_SMTP_HOST", raising=False)
    with TestClient(app) as client:
        yield client, DemoStore(database)


class PhoneCall:
    def __init__(self, client):
        self.client = client
        self.metadata = {
            "id": str(uuid4()),
            "type": "inboundPhoneCall",
            "customer": {"number": CALLER},
        }
        self.messages = [{"role": "assistant", "content": "How can I help?"}]

    def event(self, kind, **data):
        reply = self.client.post(
            "/vapi/events",
            headers=HEADERS,
            json={"message": {"type": kind, "call": self.metadata, **data}},
        )
        assert reply.status_code == 204

    def request(self, *, stream=False):
        reply = self.client.post(
            "/vapi/chat/completions",
            headers=HEADERS,
            json={"stream": stream, "call": self.metadata, "messages": self.messages},
        )
        assert reply.status_code == 200, reply.text
        if stream:
            assert reply.headers["content-type"].startswith("text/event-stream")
            chunks = [line[6:] for line in reply.text.splitlines() if line.startswith("data: ")]
            assert chunks[-1] == "[DONE]"
            assert json.loads(chunks[-2])["choices"][0]["finish_reason"] == "stop"
            return json.loads(chunks[0])["choices"][0]["delta"]["content"]
        return reply.json()["choices"][0]["message"]["content"]

    def say(self, text):
        self.messages.append({"role": "user", "content": text})
        reply = self.request(stream=True)
        assert self.request() == reply
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def heard(self, text, *, interrupted=False, reversed_delivery=False):
        turn = len(self.messages) // 2
        events = [
            ("assistant.speechStarted", {"source": "model", "text": text, "turn": turn}),
            ("speech-update", {"status": "stopped", "role": "assistant", "turn": turn}),
        ]
        for kind, payload in reversed(events) if reversed_delivery else events:
            self.event(kind, **payload)
        if interrupted:
            self.event("user-interrupted")

    def offer(self):
        self.say("Book a follow-up this Saturday")
        self.say("For me.")
        self.say("Maria Lopez")
        assert "eight digits" in self.say("Use the keypad")
        identity = self.say("User's Keypad Entry: 19840702")
        self.heard(identity)
        options = self.say("That's right")
        assert "Option 2" in options and "Option 3" not in options
        return self.say("I'll take the second option")


def test_phone_booking_requires_uninterrupted_playback_and_handles_retries(phone):
    client, store = phone
    call = PhoneCall(client)
    proposal = call.offer()
    assert "Please say yes or no." in proposal
    call.heard(proposal, interrupted=True)
    proposal = call.say("Yes")
    assert "Please say yes or no." in proposal
    assert not asyncio.run(store.search("Appointment"))
    call.heard(proposal, reversed_delivery=True)
    assert "booked" in call.say("Yes, go ahead").lower()
    (appointment,) = asyncio.run(store.search("Appointment"))
    assert appointment["status"] == "booked"
    call.event("status-update", status="ended")
    assert "ended" in call.say("Yes").lower()
    operations = client.get("/api/operations", headers={"Authorization": f"Bearer {SECRET}"})
    data = operations.json()
    assert any(
        item["transport"] == "vapi" and item["outcome"] == "booked" for item in data["recent"]
    )
    (notice,) = data["notices"]
    assert notice["status"] == "captured"
    assert not asyncio.run(store.search("Slot", status="busy-tentative"))


def test_phone_webhook_auth_and_hangup_release_the_hold(phone):
    client, store = phone
    assert client.post("/vapi/chat/completions", json={}).status_code == 401
    assert client.post("/vapi/events", json={}).status_code == 401
    assert client.post("/vapi/chat/completions", headers=HEADERS, json={}).status_code == 400
    denied = PhoneCall(client)
    denied.metadata["customer"]["number"] = "+12025550124"
    assert "configured test numbers" in denied.say("Book a follow-up")
    assert app.state.sessions == 0

    call = PhoneCall(client)
    call.offer()
    assert asyncio.run(store.search("Slot", status="busy-tentative"))
    call.event("status-update", status="ended")
    assert not asyncio.run(store.search("Slot", status="busy-tentative"))
    assert app.state.sessions == 0

    staff = PhoneCall(client)
    assert "front desk" in staff.say("I'd like to speak to the front desk")
    operations = client.get("/api/operations", headers={"Authorization": f"Bearer {SECRET}"})
    (task,) = operations.json()["tasks"]
    assert task["reason"] == "staff_requested"


def test_phone_can_book_a_first_visit_for_a_new_patient(phone):
    client, store = phone
    call = PhoneCall(client)
    call.say("I'm a new patient and I'd like to book a first visit this Saturday")
    call.say("For me.")
    call.say("Nora Marlow")
    assert "What year" in call.say("Sixteenth of August")
    assert "What year" in call.say("Sorry, can you hear me?")
    identity = call.say("I was born in two thousand and one")
    assert "Nora Marlow" in identity
    call.heard(identity)
    options = call.say("Yes, that's right")
    assert "Option 2" in options and "Option 3" not in options
    assert "email address" in call.say("I'll take option two")
    email = call.say("nora@example.test")
    call.heard(email)
    proposal = call.say("That's correct")
    assert "book your first visit" in proposal
    assert not asyncio.run(store.search("Patient", family="Marlow"))
    call.heard(proposal)
    assert "booked" in call.say("Go ahead").lower()
    (patient,) = asyncio.run(store.search("Patient", family="Marlow"))
    assert patient["birthDate"] == "2001-08-16"
    (appointment,) = asyncio.run(store.search("Appointment", patient=f"Patient/{patient['id']}"))
    assert appointment["status"] == "booked"


def test_phone_historical_stroke_keeps_booking_open_and_acute_report_releases_hold(phone):
    client, store = phone
    call = PhoneCall(client)
    assert "for you or for someone else" in call.say(
        "My mom had a stroke last year. I'd like to book a follow-up."
    )
    assert "name" in call.say("For me.")
    call.say("Maria Lopez")
    identity = call.say("7/2/84")
    call.heard(identity)
    options = call.say("Yep, that's me")
    assert "Option 2" in options and "Option 3" not in options
    assert "Please say yes or no" in call.say("Option one")
    (held,) = asyncio.run(store.search("Slot", status="busy-tentative"))
    assert "911" in call.say("My husband just collapsed and won't wake up.")
    assert asyncio.run(store.search("Appointment")) == []
    assert asyncio.run(store.read("Slot", held["id"]))["status"] == "free"


@pytest.mark.parametrize(
    "opening",
    [
        "My relative had a stroke last year and I wanted to schedule a follow-up for that.",
        "My grandfather had surgery. Can I book a follow-up for him?",
        "Can I schedule a visit on behalf of my aunt?",
    ],
)
def test_phone_establishes_who_the_visit_is_for_before_asking_for_identity(phone, opening):
    client, store = phone
    call = PhoneCall(client)
    reply = call.say(opening)
    if "for you or for someone else" in reply:
        reply = call.say("For my relative.")
    assert "permission" in reply
    assert "patient's full name" in call.say("Yes, please")
    assert asyncio.run(store.search("Appointment")) == []


@pytest.mark.parametrize(
    "spoken_date",
    [
        "26 March 1956",
        "the twenty-sixth of March, nineteen fifty-six",
        "March 26, 19 56",
        "26/03/1956",
        "March twenty six nineteen fifty six",
    ],
)
def test_phone_accepts_spoken_dob_and_explains_unconfirmed_playback(phone, spoken_date):
    client, store = phone
    call = PhoneCall(client)
    call.say("Book a first visit")
    call.say("For me.")
    call.say("Cora Bell")
    prompt = call.say(spoken_date)
    assert "March 26, 1956" in prompt
    retry = call.say("Yes")
    assert "I have the date" in retry
    assert "March 26, 1956" in retry
    assert asyncio.run(store.search("Appointment")) == []
    call.heard(retry)
    assert "Option 1" in call.say("Yes")


def test_phone_routes_repeated_missing_readback_to_staff(phone):
    client, store = phone
    call = PhoneCall(client)
    call.say("Book an appointment")
    call.say("For me.")
    call.say("Maria Lopez")
    assert "July 2, 1984" in call.say("July two nineteen eighty-four")
    assert "I have the date" in call.say("Yes")
    assert "I have the date" in call.say("Yes")
    assert "front desk can help" in call.say("Yes")
    assert asyncio.run(store.search("Appointment")) == []


def test_phone_does_not_take_a_name_as_the_booking_recipient(phone):
    client, store = phone
    call = PhoneCall(client)
    assert "for you or for someone else" in call.say(
        "They asked me to arrange the follow-up. Can you book it?"
    )
    assert "for you or for someone else" in call.say("Cora Bell")
    assert "for you or for someone else" in call.say("Yes")
    assert "permission" in call.say("For him")
    assert "patient's full name" in call.say("Yes, please")
    assert asyncio.run(store.search("Appointment")) == []


def test_phone_accepts_stroke_followup_without_a_date_and_keeps_emergency_override(phone):
    client, store = phone
    call = PhoneCall(client)
    assert "for you or for someone else" in call.say(
        "Can I schedule a follow-up meeting with my uncle for his stroke?"
    )
    assert "permission" in call.say("For him")
    assert "911" in call.say("Wait. He's gasping for air.")
    assert asyncio.run(store.search("Appointment")) == []


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "invalid_result"])
def test_unresolved_opening_recovers_without_guessing_the_request(phone, monkeypatch, failure):
    from switchboard.intents import TurnMeaning

    client, store = phone
    attempts = 0

    class Interpreter:
        async def interpret_turn(self, context, actions):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                if failure == "timeout":
                    raise TimeoutError
                return None
            if failure == "invalid_result":
                return None
            return TurnMeaning(action="book", value="", emergency=False, needs_staff=False)

    monkeypatch.setattr(app.state, "turn_interpreter", Interpreter())
    call = PhoneCall(client)
    assert "could you repeat" in call.say("They told me to ring and sort it out.").lower()
    response = call.say("The clinic told me to get something into the diary.")
    if failure == "invalid_result":
        assert "front desk can help" in response
    else:
        assert "for you or for someone else" in response
    assert asyncio.run(store.search("Appointment")) == []


def test_recipient_answer_does_not_skip_handoff_consent_on_model_misclassification(
    phone, monkeypatch
):
    from switchboard.intents import TurnMeaning

    class Interpreter:
        async def interpret_turn(self, context, actions):
            action = "staff" if context["stage"] == "booking_recipient" else "unclear"
            return TurnMeaning(action=action, value="", emergency=False, needs_staff=True)

    client, _ = phone
    monkeypatch.setattr(app.state, "turn_interpreter", Interpreter())
    call = PhoneCall(client)
    assert "for you or for someone else" in call.say("Can I book an appointment?")
    assert "permission" in call.say("For him.").lower()
    assert "haven't made any changes" in call.say("No, thank you.")


def spoken_confirmation(prompt):
    match = re.search(r"To confirm, say code ([^.]+)\.", prompt)
    assert match, prompt
    return "Code " + match[1]


@pytest.mark.parametrize("proxy", [False, True])
def test_phone_books_when_vapi_never_sends_playback_callbacks(phone, proxy):
    client, store = phone
    call = PhoneCall(client)
    if proxy:
        assert "permission" in call.say("Book a follow-up for my uncle next week")
        assert "patient's full name" in call.say("Yes")
    else:
        assert "for you" in call.say("Book a follow-up next week")
        call.say("For me.")
    dob_prompt = call.say("Maria Lopez")
    assert ("patient's date of birth" if proxy else "your date of birth") in dob_prompt
    assert "July 2, 1984" in call.say("July second nineteen eighty-four")
    identity_code = spoken_confirmation(call.say("Yes"))
    assert "Option 2" in call.say(identity_code)
    assert "Please say yes or no" in call.say("Option one")
    booking_code = spoken_confirmation(call.say("Yes"))
    assert not asyncio.run(store.search("Appointment"))
    fresh_code = spoken_confirmation(call.say(identity_code))
    assert fresh_code != booking_code and fresh_code != identity_code
    assert "booked" in call.say(fresh_code).lower()
    (booked,) = asyncio.run(store.search("Appointment", patient="Patient/maria-demo"))
    assert booked["status"] == "booked"
    operations = client.get("/api/operations", headers={"Authorization": f"Bearer {SECRET}"}).json()
    assert bool(operations["notices"]) is not proxy
