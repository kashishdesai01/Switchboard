import asyncio
import json
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
        self.say("Maria Lopez")
        assert "eight digits" in self.say("Use the keypad")
        identity = self.say("User's Keypad Entry: 19840702")
        self.heard(identity)
        assert "Option 3" in self.say("That's right")
        return self.say("I'll take the third option")


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
    call.say("Nora Marlow")
    assert "What year" in call.say("Sixteenth of August")
    assert "What year" in call.say("Sorry, can you hear me?")
    identity = call.say("I was born in two thousand and one")
    assert "Nora Marlow" in identity
    call.heard(identity)
    assert "Option 3" in call.say("Yes, that's right")
    assert "email address" in call.say("I'll take option three")
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
