import asyncio
import json
import time as time_module
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from switchboard.api import app
from switchboard.config import BAYVIEW
from switchboard.frontdesk import FrontDesk, MailSettings
from switchboard.intents import ClaudeRouter
from switchboard.scheduling import instant, utc_now

TOKEN = "synthetic-front-desk-staff-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def next_wednesday_morning() -> datetime:
    # The clinic refuses changes inside cancel_notice_hours. Tests that book "next week"
    # and then change the visit need it to be days away, which the real clock can't
    # promise: on a Sunday "next Monday" is tomorrow morning.
    today = utc_now().astimezone(BAYVIEW.timezone).date()
    wednesday = today + timedelta(days=(2 - today.weekday()) % 7 or 7)
    return datetime.combine(wednesday, time(9), BAYVIEW.timezone).astimezone(UTC)


NOW = next_wednesday_morning()


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "frontdesk.sqlite"))
    monkeypatch.setenv("SWITCHBOARD_OPERATIONS_TOKEN", TOKEN)
    monkeypatch.delenv("SWITCHBOARD_SMTP_HOST", raising=False)
    monkeypatch.setattr("switchboard.api.utc_now", lambda: NOW)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.scheduling, "now", lambda: NOW)
        yield client


@contextmanager
def conversation(client):
    with client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket:
        socket.receive_json()
        socket.receive_json()

        def say(text, reply_to=None):
            socket.send_json({"type": "text", "text": text, "reply_to": reply_to})
            return socket.receive_json()

        yield say


def verify(say, name="Maria Lopez", dob="July 2, 1984"):
    say(name)
    challenge = say(dob)
    return say("yes", challenge["id"])


def test_opening_name_today_alternatives_and_conversational_consent(desk, monkeypatch):
    after_close = NOW.astimezone(BAYVIEW.timezone).replace(hour=18)
    monkeypatch.setattr(app.state.scheduling, "now", lambda: after_close)
    with conversation(desk) as say:
        name = say("Hi. My name is Asha Mercer. Can I book with the doctor today?")
        name = say("For me.")
        assert "I heard your name as Asha Mercer" in name["text"]
        assert "date of birth" in say("Yeah. Sure.", name["id"])["text"]
        identity = say("18 February 1993")
        today = say("Okay, that's right", identity["id"])
        assert "opening today" in today["text"]
        assert today["calendar"] == []

        offered = say("Ya shore.")
        assert offered["calendar"]
        more = say("Can you show me some other times?")
        assert more["calendar"]
        assert {item["label"] for item in offered["calendar"]}.isdisjoint(
            item["label"] for item in more["calendar"]
        )
        assert "What email address" in say("the first one works")["text"]
        email = say("asha@example.test")
        held = say("yes", email["id"])
        assert held["confirmation"]
        changed = say("Yes, but after ten", held["id"])
        assert changed["outcome"] is None
        assert all(item["state"] == "offered" for item in changed["calendar"])
        held = say("the first one works")
        assert say("Okay, go ahead", held["id"])["outcome"] == "booked"


def test_unknown_provider_and_sentence_like_name_recovery(desk):
    with conversation(desk) as say:
        assert "for you" in say("Book with Doctor Rivera next week")["text"]
        assert "Dr. Rivera" in say("For me.")["text"]
        assert "another doctor" in say("I haven't decided yet")["text"]
        assert "What name" in say("Yeah. Sure.")["text"]
        invalid = say("That should stay safe.")
        assert "repeat it or spell it" in invalid["text"]
        assert "date of birth" in say("T U S H I T A")["text"]
        identity = say("May 18, 1993")
        assert "Tushita" in identity["text"]
        assert "Option 1" in say("Yeah, that's correct.", identity["id"])["text"]


def test_opening_name_survives_a_transcript_split_before_the_request(desk):
    with conversation(desk) as say:
        say("Hi, my name is Tariq Sandoval.")
        reply = say("Can I book with the doctor tomorrow?")
        reply = say("For me.")
        assert "I heard your name as Tariq Sandoval" in reply["text"]
        assert reply["confirmation"]


def test_booking_and_reschedule_notices_follow_committed_calendar(desk):
    with conversation(desk) as say:
        initial = say(
            "Book next week at Downtown. My name is Theo Van Buren and my date of birth is "
            "the 17th of March 1990."
        )
        initial = say("For me.")
        assert "Theo Van Buren, born March 17, 1990" in initial["text"]
        offered = say("yes", initial["id"])
        assert offered["calendar"][0]["state"] == "offered"
        options = say("Actually, between ten and noon")
        assert options["calendar"]
        assert all(
            "Downtown Clinic" in slot["label"] and 10 <= instant(slot["start"]).hour <= 12
            for slot in options["calendar"]
        )
        assert "What email address" in say("The second one works for me")["text"]
        address = say("theo@example.test")
        proposed = say("yes", address["id"])
        assert proposed["calendar"][0]["state"] == "held"
        assert proposed["email_status"] is None
        invalid = say("yes", initial["id"])
        assert invalid["outcome"] is None
        booked = say("yes please book it", invalid["id"])
        assert booked["outcome"] == "booked"
        assert booked["calendar"][0]["state"] == "booked"

    assert booked["email_status"] == "captured"

    with conversation(desk) as say:
        say("Please reschedule to next week in the afternoon at Westside")
        current = verify(say, "Theo Van Buren", "March 17, 1990")
        assert "Downtown Clinic" in current["text"]
        offers = say("one")
        assert all("Westside Clinic" in slot["label"] for slot in offers["calendar"])
        proposed = say("option one")
        moved = say("yes, please move it", proposed["id"])
        assert moved["outcome"] == "rescheduled"
    assert moved["email_status"] == "captured"

    report = desk.get("/api/operations", headers=AUTH).json()
    assert len(report["notices"]) == 2
    by_subject = {notice["subject"]: notice["body"] for notice in report["notices"]}
    assert "Downtown Clinic" in by_subject["Your appointment confirmation"]
    assert "Westside Clinic" in by_subject["Your updated appointment"]
    assert "America/Los_Angeles" in by_subject["Your updated appointment"]
    assert "theo@example.test" not in json.dumps(report)


def test_referral_eligibility_and_actionable_staff_queue(desk):
    with conversation(desk) as say:
        say("I'd like to see a spine specialist")
        say("For me.")
        denied = verify(say, dob="February 14, 1991")
        assert denied["outcome"] == "handoff"
        assert "requires a referral" in denied["text"]

    assert desk.get("/api/operations").status_code == 401
    data = desk.get("/api/operations", headers=AUTH).json()
    (item,) = data["tasks"]
    assert item["reason"] == "referral_required"
    assert item["context"]["patient_reference"] == "Patient/maria-namesake"
    assert item["context"]["service"] == "spine"
    assert "verify the referral" in item["next_action"].casefold()
    assert "Maria Lopez" not in json.dumps(data)

    path = f"/api/operations/work/{item['id']}"
    assert desk.get(path + "/context").status_code == 401
    current = desk.get(path + "/context", headers=AUTH).json()
    assert current["Active referral"] == "Needs verification"
    claim = {"action": "claim", "revision": item["revision"], "value": "Casey"}
    assert desk.post(path, json=claim).status_code == 401
    assert desk.post(path, headers=AUTH, json=claim).status_code == 204
    assert desk.post(path, headers=AUTH, json=claim).status_code == 409
    resolve = {"action": "resolve", "revision": item["revision"] + 1, "value": "Patient contacted"}
    assert desk.post(path, headers=AUTH, json=resolve).status_code == 204
    (completed,) = desk.get("/api/operations", headers=AUTH).json()["tasks"]
    assert (completed["status"], completed["assignee"], completed["resolution"]) == (
        "resolved",
        "Casey",
        "Patient contacted",
    )

    with conversation(desk) as say:
        say("Can you schedule a spine consultation next week?")
        say("For me.")
        approved = verify(say)
        assert approved["calendar"]
        assert all("Dr. Priya Patel" in slot["label"] for slot in approved["calendar"])


def test_waitlist_offer_matches_released_slot_and_is_idempotent(desk):
    today = NOW.astimezone(BAYVIEW.timezone).date()
    monday = today + timedelta(days=7 - today.weekday())
    target = monday.strftime("%B %d")
    with conversation(desk) as say:
        say(f"Book a follow-up on {target} at Downtown after ten")
        say("For me.")
        options = verify(say)
        chosen = say("option one")
        assert options["calendar"][0]["start"] == chosen["calendar"][0]["start"]
        assert say("yes", chosen["id"])["outcome"] == "booked"

    with conversation(desk) as say:
        say(f"Book a follow-up on {target} at Downtown after ten")
        say("For me.")
        verify(say, dob="February 14, 1991")
        assert say("Please add me to the waitlist")["outcome"] == "waitlisted"

    with conversation(desk) as say:
        say("Cancel my appointment")
        verify(say)
        cancel = say("option one")
        assert say("yes", cancel["id"])["outcome"] == "cancelled"

    desk.portal.call(app.state.frontdesk.match_waitlist, app.state.scheduling)
    desk.portal.call(app.state.frontdesk.match_waitlist, app.state.scheduling)
    data = desk.get("/api/operations", headers=AUTH).json()
    (offer,) = data["tasks"]
    assert offer["reason"] == "waitlist_offer"
    assert offer["context"]["location"] == "downtown"
    assert offer["context"]["start"] == chosen["calendar"][0]["start"]
    assert offer["context"]["patient_reference"] == "Patient/maria-namesake"
    (entry,) = data["waitlist"]
    assert entry["status"] == "offered"


def test_smtp_worker_delivers_once_automatically(desk, monkeypatch):
    delivered = []
    monkeypatch.setattr(MailSettings, "send", lambda self, notice: delivered.append(notice))
    app.state.frontdesk.mail = MailSettings(
        host="mail.example.test", allowed=frozenset({"maria.lopez@example.test"})
    )
    with conversation(desk) as say:
        say("Book a follow-up next week")
        say("For me.")
        verify(say)
        proposal = say("one")
        assert "email on file" in proposal["text"]
        booked = say("yes", proposal["id"])
        assert booked["outcome"] == "booked"
        assert booked["email_status"] in {"queued", "sending", "sent"}

    desk.portal.call(app.state.frontdesk.deliver, app.state.scheduling)
    desk.portal.call(app.state.frontdesk.deliver, app.state.scheduling)
    (message,) = delivered
    assert message["recipient"] == "maria.lopez@example.test"
    assert message["message_id"]
    assert "Maria" not in message["body"]


def test_spoken_email_readback_requires_separate_appointment_consent(desk):
    with conversation(desk) as say:
        say("Book a first visit next week")
        say("For me.")
        verify(say, "Frida Tran", "17th July 1995")
        hold = say("the first one works")
        assert "What email address" in hold["text"]
        email = say("frida dot tran at example dot test")
        assert "frida dot tran at example dot test" in email["text"]
        assert say("yes", hold["id"])["outcome"] is None
        email = say("frida dot tran at example dot test")
        next_confirmation = say("yes", email["id"])
        assert next_confirmation["outcome"] is None
        assert "book your first visit" in next_confirmation["text"]
        assert desk.get("/api/operations", headers=AUTH).json()["notices"] == []
        booked = say("yes", next_confirmation["id"])
        assert booked["outcome"] == "booked"
        assert "saved to the demo outbox" in booked["text"]


def test_uncertain_smtp_delivery_is_recoverable_as_staff_work(desk, monkeypatch):
    delivered = []
    monkeypatch.setattr(MailSettings, "send", lambda self, notice: delivered.append(notice))
    settings = MailSettings(
        host="mail.example.test", allowed=frozenset({"maria.lopez@example.test"})
    )
    app.state.frontdesk.mail = settings
    with conversation(desk) as say:
        say("Book next week")
        say("For me.")
        verify(say)
        proposed = say("option one")
        booked = say("yes", proposed["id"])
    assert booked["email_status"] == "queued"
    with app.state.ops.connect() as connection:
        connection.execute("UPDATE appointment_notices SET status='sending'")
    restarted = FrontDesk(app.state.ops, settings)
    desk.portal.call(restarted.deliver, app.state.scheduling)
    assert delivered == []
    state = desk.portal.call(restarted.snapshot)
    assert state["notices"][0]["status"] == "delivery_unknown"
    assert state["tasks"][0]["reason"] == "email_delivery"


def test_model_extraction_requires_source_evidence():
    def provider(request):
        payload = json.loads(request.content)
        assert payload["tool_choice"]["name"] == "extract_preferences"
        utterance = payload["messages"][0]["content"]
        evidence = "once the morning rush is over" if "rush" in utterance else "never said"
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "extract_preferences",
                        "input": {"evidence": evidence, "normalized": "after 10 am"},
                    }
                ]
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            model = ClaudeRouter(client, "fixture", "fixture-model")
            assert await model.preferences("once the morning rush is over") == "after 10 am"
            assert await model.preferences("I don't know") is None

    asyncio.run(exercise())


def test_staff_calendar_reviews_conversation_and_changes_appointment(desk):
    app.state.ops.capture_transcripts = True
    with conversation(desk) as say:
        say("Book a follow-up next week with Doctor Chen")
        say("For me.")
        verify(say)
        offer = say("Can you show me some other times?")
        assert offer["calendar"]
        proposal = say("Option one")
        booked = say("yes", proposal["id"])
        assert booked["outcome"] == "booked"
    day = instant(booked["calendar"][0]["start"]).astimezone(BAYVIEW.timezone).date()
    path = "/api/operations/appointments"

    assert desk.get(path).status_code == 401
    (visit,) = desk.get(path, params={"day": str(day), "provider": "chen"}, headers=AUTH).json()[
        "appointments"
    ]
    assert visit["provider"] == "Dr. Daniel Chen"
    assert visit["patient"] == "Maria Lopez"
    assert visit["location"] == "Downtown Clinic"
    assert visit["confirmation"] == "captured"
    call_path = f"/api/operations/calls/{visit['call_id']}"
    assert desk.get(call_path).status_code == 401
    # The server saves the call record after the socket closes; wait for it briefly.
    for _ in range(50):
        detail = desk.get(call_path, headers=AUTH).json()
        if "call" in detail:
            break
        time_module.sleep(0.05)
    assert detail["call"]["outcome"] == "booked"
    assert {"speaker": "caller", "text": "Can you show me some other times?"} in detail[
        "transcript"
    ]
    assert any(
        turn["speaker"] == "assistant" and "date of birth" in turn["text"]
        for turn in detail["transcript"]
    )
    assert "transcript" not in desk.get("/api/operations", headers=AUTH).json()["recent"][0]

    original = f"{path}/{visit['id']}"
    new_day = day + timedelta(days=1)
    slots = desk.get(f"{original}/openings", params={"day": str(new_day)}, headers=AUTH).json()
    change = {"action": "reschedule", "slot": slots[0]["id"], "revision": visit["revision"]}
    assert desk.post(original, json=change, headers=AUTH).status_code == 204
    assert desk.post(original, json=change, headers=AUTH).status_code == 409
    (cancelled,) = desk.get(path, params={"day": str(day)}, headers=AUTH).json()["appointments"]
    assert cancelled["status"] == "cancelled"
    (moved,) = desk.get(path, params={"day": str(new_day)}, headers=AUTH).json()["appointments"]
    assert moved["status"] == "booked" and moved["start"] == slots[0]["start"]
    assert moved["confirmation"] == "captured"
    assert (
        desk.post(
            f"{path}/{moved['id']}",
            json={"action": "cancel", "revision": moved["revision"]},
            headers=AUTH,
        ).status_code
        == 204
    )
    (cancelled,) = desk.get(path, params={"day": str(new_day)}, headers=AUTH).json()["appointments"]
    assert cancelled["status"] == "cancelled"


@pytest.mark.parametrize("selection_style", ["spoken time", "date and time", "please book"])
def test_phone_style_slot_selection_keeps_the_requested_doctor(desk, selection_style):
    today = NOW.astimezone(BAYVIEW.timezone).date()
    first = today.replace(day=14)
    if first <= today:
        first = (today.replace(day=28) + timedelta(days=4)).replace(day=14)
    second = first + timedelta(days=1)
    with conversation(desk) as say:
        say("Hi. Can I book a meeting with schedule a meeting with doctor Chen?")
        say("For me.")
        say("Nora Sato")
        ambiguous = say("It's fifteenth March twenty twenty fifteenth March two thousand.")
        assert "two different birth dates" in ambiguous["text"]
        identity = say("03/15/1999.")
        say("Yes", identity["id"])
        say("I see.")
        reply = say(f"Do you have any slots available for fourteenth or fifteenth of {first:%B}?")
        options = reply["calendar"]
        assert {instant(slot["start"]).date() for slot in options} == {first, second}
        assert all("Dr. Daniel Chen" in slot["label"] for slot in options)
        # A time-only reply must refer to exactly one offer. On some calendar
        # dates both requested days have the same first opening.
        slot = options[0]
        if selection_style == "spoken time":
            slots_by_clock = [
                instant(candidate["start"]).astimezone(BAYVIEW.timezone).time()
                for candidate in options
            ]
            slot = next(
                candidate
                for candidate, clock in zip(options, slots_by_clock, strict=True)
                if slots_by_clock.count(clock) == 1
            )
        start = instant(slot["start"]).astimezone(BAYVIEW.timezone)
        selection = {
            "spoken time": f"I think {start:%I:%M%p} books will",
            "date and time": f"Yes. I meant {start:%B %d, %I:%M%p} openings would be fine.",
            "please book": "Please book option one.",
        }[selection_style]
        email = say(selection)
        assert "email address" in email["text"]
        assert email["calendar"][0]["start"] == slot["start"]
        proposal = say("skip email")
        assert proposal["confirmation"]
        assert say("Yes, go ahead", proposal["id"])["outcome"] == "booked"


def test_contextual_choice_uses_offers_and_requires_separate_confirmation(desk, monkeypatch):
    stages = []

    def provider(request):
        payload = json.loads(request.content)
        assert payload["tool_choice"]["name"] == "interpret_clinic_turn"
        # The model input is labelled lines; options and the reply are JSON.
        lines = payload["messages"][0]["content"].splitlines()
        context = {"stage": lines[0].removeprefix("Stage: ")}
        for line in lines:
            if line.startswith("Offered options: "):
                context["options"] = json.loads(line.removeprefix("Offered options: "))
            if line.startswith("The caller replied: "):
                context["caller"] = json.loads(line.removeprefix("The caller replied: "))
        stages.append(context["stage"])
        action, value = "unclear", ""
        if context["stage"] == "name":
            action, value = "name", context["caller"]
        if context["stage"] == "select_slot":
            assert "Dr. Daniel Chen" in context["options"]["2"]
            assert "Maria Lopez" not in payload["messages"][0]["content"]
            if context["caller"].startswith("Put me down"):
                action, value = "select", "2"
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "interpret_clinic_turn",
                        "input": {
                            "action": action,
                            "value": value,
                            "emergency": False,
                            "needs_staff": False,
                        },
                    }
                ]
            },
        )

    model_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(
        app.state, "browser_router", ClaudeRouter(model_client, "fixture", "fixture")
    )
    try:
        with conversation(desk) as say:
            say("Book a follow-up next week with Doctor Chen")
            say("For me.")
            offers = verify(say)
            unclear = say("Those might work.")
            assert all(slot["state"] == "offered" for slot in unclear["calendar"])
            chosen = say("Put me down for the one you mentioned between the other two.")
            assert chosen["confirmation"]
            assert chosen["calendar"][0]["start"] == offers["calendar"][1]["start"]
            assert not asyncio.run(app.state.scheduling.store.search("Appointment"))
            assert say("Yes, go ahead.", chosen["id"])["outcome"] == "booked"
            assert stages == [
                "intent",
                "booking_recipient",
                "name",
                "dob",
                "check_dob",
                "select_slot",
                "select_slot",
                "confirm_change",
            ]
    finally:
        desk.portal.call(model_client.aclose)


def test_natural_opening_mixed_billing_and_lunch_refinement(desk):
    with conversation(desk) as say:
        assert (
            "for you"
            in say(
                "I have a billing question, but my knee has been hurting and I'd like to see a doctor next week"
            )["text"]
        )
        assert "name" in say("For me.")["text"]
        say("Maria Lopez")
        identity = say("7/2/84")
        assert "July 2, 1984" in identity["text"]
        offered = say("Yep, that's me", identity["id"])
        later = say("Anything closer to lunch?")
        assert offered["calendar"][0]["start"] != later["calendar"][0]["start"]
        held = say("option one")
        assert say("Yes, go ahead.", held["id"])["outcome"] == "booked"
    (task,) = desk.get("/api/operations", headers=AUTH).json()["tasks"]
    assert task["reason"] == "staff_requested"
    assert task["context"]["topic"] == "billing"


def test_proxy_request_requires_permission_before_collecting_patient_identity(desk):
    with conversation(desk) as say:
        reply = say("Can I make an appointment for my son next week?")
        assert reply["outcome"] is None
        assert "permission" in reply["text"]
        assert say("No")["outcome"] == "declined"
    assert not asyncio.run(app.state.scheduling.store.search("Appointment"))


@pytest.mark.parametrize(
    "utterance",
    [
        "I want to change the insurance on file",
        "I'm running late for my 2pm",
        "Can I update my coverage with a new card?",
        "My visit is in twenty minutes and I may be late for it.",
        "I'm running a few minutes late for my appointment.",
    ],
)
def test_admin_request_does_not_start_a_booking(desk, utterance):
    with conversation(desk) as say:
        assert say(utterance)["outcome"] == "handoff"


def test_visit_request_with_symptoms_works_without_model(desk):
    with conversation(desk) as say:
        reply = say(
            "I'd like a visit with Dr. Chen next week. My symptoms have been getting worse."
        )
        assert reply["outcome"] is None
        assert "for you" in reply["text"]
        assert "name" in say("For me.")["text"]
