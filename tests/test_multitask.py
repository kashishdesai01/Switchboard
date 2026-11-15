"""One call can handle several tasks, and only a goodbye, emergency or handoff ends it."""

from fastapi.testclient import TestClient
from test_gaps import Clinic, run

from switchboard.api import app
from switchboard.conversation import MAX_TASKS


async def book_for_self(session) -> None:
    await session.say("book a follow-up next week")
    await session.say("For me.")
    reply = await session.verify_maria()
    assert "Option 1" in reply.text, reply.text
    await session.say("one")
    reply = await session.say("yes")
    assert reply.outcome == "booked", reply.text


def test_hours_then_booking_in_one_call(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        reply = await session.say("what are your hours?")
        assert reply.outcome == "info_only"
        assert not reply.ends_call
        assert reply.text.endswith("Is there anything else I can help with?")
        await book_for_self(session)
        assert len(await clinic.booked()) == 1

    run(scenario())


def test_reschedule_after_booking_skips_verification(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        await book_for_self(session)
        [booked] = await clinic.booked()
        reply = await session.say("actually, can I move that appointment")
        assert "name" not in reply.text.lower(), reply.text
        assert reply.text.startswith("Which existing appointment"), reply.text
        await session.say("one")
        await session.say("one")
        reply = await session.say("yes")
        assert reply.outcome == "rescheduled", reply.text
        [moved] = await clinic.booked()
        assert moved["start"] != booked["start"]
        assert session.trace.outcomes == ["booked", "rescheduled"]
        assert session.trace.outcome == "rescheduled"

    run(scenario())


def test_booking_for_someone_else_verifies_again(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        await book_for_self(session)
        reply = await session.say("I also need to book for my son")
        assert "permission" in reply.text.lower(), reply.text
        reply = await session.say("yes")
        assert reply.text == "What's the patient's full name?"

    run(scenario())


def test_no_thanks_ends_the_call(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        await session.say("do you accept Aetna insurance?")
        reply = await session.say("no thanks")
        assert reply.ends_call
        assert reply.text == "Thanks for calling. Goodbye."
        assert session.call.stage == "closed"
        assert session.trace.outcome == "info_only"

    run(scenario())


def test_no_with_a_new_request_continues(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path, existing=1)
        session = clinic.session()
        await session.say("what are your hours?")
        reply = await session.say("no, but can I also cancel my appointment")
        assert not reply.ends_call
        assert reply.text == "What name should I use?", reply.text

    run(scenario())


def test_emergency_after_a_task_routes_as_emergency(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        await session.say("what are your hours?")
        reply = await session.say("my husband just collapsed and isn't breathing")
        assert reply.outcome == "emergency"
        assert reply.ends_call
        assert session.trace.outcome == "emergency"
        assert session.trace.work[0] == "emergency"

    run(scenario())


def test_task_cap_hands_off(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        for _ in range(MAX_TASKS):
            reply = await session.say("what are your hours?")
            assert reply.outcome == "info_only"
        reply = await session.say("what are your hours?")
        assert reply.outcome == "handoff"
        assert reply.ends_call

    run(scenario())


def test_trace_outcome_prefers_handoff_over_later_tasks(tmp_path):
    async def scenario():
        clinic = Clinic(tmp_path)
        session = clinic.session()
        await session.say("what are your hours?")
        reply = await session.say("I need a human")
        assert reply.outcome == "handoff"
        assert session.trace.outcomes == ["info_only", "handoff"]
        assert session.trace.outcome == "handoff"
        assert session.call.summary_outcome() == "handoff"

    run(scenario())


def test_browser_socket_stays_open_until_goodbye(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "browser.sqlite"))
    with TestClient(app) as client:
        with client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket:
            socket.receive_json()
            socket.receive_json()

            def say(text):
                socket.send_json({"type": "text", "text": text})
                return socket.receive_json()

            reply = say("what are your hours?")
            assert reply["outcome"] == "info_only"
            assert not reply["ends_call"]
            reply = say("do you accept Aetna insurance?")
            assert reply["outcome"] == "info_only"
            reply = say("that's all, bye")
            assert reply["ends_call"]
            assert socket.receive()["type"] == "websocket.close"
