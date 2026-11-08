import asyncio
from datetime import date, time, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from switchboard.api import app
from switchboard.config import BAYVIEW
from switchboard.conversation import parse_date
from switchboard.scheduling import instant, utc_now
from switchboard.store import DemoStore


def test_browser_streaming_skips_token_only_on_loopback(tmp_path, monkeypatch):
    async def skip_filler(*args):
        pass

    class StubVoiceCall:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return "closed"

    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "voice.sqlite"))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fixture-key")
    monkeypatch.setenv("SWITCHBOARD_BROWSER_AUDIO_TOKEN", "fixture-token-for-shared-demo")
    monkeypatch.setattr("switchboard.api.warm_filler", skip_filler)
    monkeypatch.setattr("switchboard.browser_audio.VoiceCall", StubVoiceCall)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/capabilities").json()["streaming_audio_token_required"] is False
        with client.websocket_connect(
            "ws://127.0.0.1/voice-lab", headers={"origin": "http://127.0.0.1"}
        ) as ws:
            assert ws.receive_json()["type"] == "ready"
    with TestClient(app, base_url="https://shared.example") as client:
        assert client.get("/capabilities").json()["streaming_audio_token_required"] is True
        with client.websocket_connect(
            "wss://shared.example/voice-lab", headers={"origin": "https://shared.example"}
        ) as ws:
            ws.send_text("wrong-token")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008


def test_browser_call_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "browser.sqlite"))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        with client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket:
            assert socket.receive_json()["mode"] == "Local simulator"
            assert socket.receive_json()["type"] == "reply"

            def say(text):
                socket.send_json({"type": "text", "text": text})
                return socket.receive_json()

            say("book a first visit")
            say("Maria Lopez")
            dob = say("1984-07-02")
            socket.send_json({"type": "displayed", "id": dob["id"]})
            assert "Option 1" in say("yes")["text"]
            proposed = say("option one")
            assert proposed["confirmation"]
            repeated = say("yes")
            assert repeated["confirmation"]
            socket.send_json({"type": "displayed", "id": proposed["id"]})
            repeated = say("yes please book it")
            assert repeated["confirmation"]
            socket.send_json({"type": "displayed", "id": repeated["id"]})
            repeated = say("yes but don't book it")
            assert repeated["confirmation"]
            socket.send_json({"type": "displayed", "id": repeated["id"]})
            assert say("yes please book it")["outcome"] == "booked"


def test_third_weekend_option_books_the_displayed_doctor_and_time(tmp_path, monkeypatch):
    database = tmp_path / "weekend.sqlite"
    monkeypatch.setenv("SWITCHBOARD_DB", str(database))
    today = utc_now().astimezone(BAYVIEW.timezone).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    with (
        TestClient(app) as client,
        client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket,
    ):
        socket.receive_json()
        socket.receive_json()

        def say(text, reply_to=None):
            socket.send_json({"type": "text", "text": text, "reply_to": reply_to})
            return socket.receive_json()

        say("Book a follow-up this Saturday")
        say("Maria Lopez")
        identity = say("July 2, 1984")
        offered = say("That's correct", identity["id"])["calendar"]
        assert len(offered) == 3
        assert len({slot["start"] for slot in offered}) == 3
        for slot in offered:
            start = instant(slot["start"]).astimezone(BAYVIEW.timezone)
            assert start.date() == saturday and time(9) <= start.time() < time(17)
        proposal = say("I'll take the third option")
        assert proposal["calendar"][0]["start"] == offered[2]["start"]
        assert say("Go ahead", proposal["id"])["outcome"] == "booked"

    (appointment,) = asyncio.run(DemoStore(database).search("Appointment"))
    assert appointment["start"] == offered[2]["start"]
    (doctor,) = [
        item["actor"]["reference"].split("/")[-1]
        for item in appointment["participant"]
        if item["actor"]["reference"].startswith("Practitioner/")
    ]
    assert BAYVIEW.providers[doctor] in offered[2]["label"]


@pytest.mark.parametrize("legacy_client", [False, True])
def test_requested_time_and_natural_selection(tmp_path, monkeypatch, legacy_client):
    database = tmp_path / "browser.sqlite"
    monkeypatch.setenv("SWITCHBOARD_DB", str(database))
    today = utc_now().astimezone(BAYVIEW.timezone).date()
    friday = today + timedelta(days=7 - today.weekday() + 4)
    with (
        TestClient(app) as client,
        client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket,
    ):
        socket.receive_json()
        socket.receive_json()

        def say(text, reply_to=None):
            socket.send_json({"type": "text", "text": text, "reply_to": reply_to})
            return socket.receive_json()

        def confirm(reply):
            assert reply["confirmation"]
            if legacy_client:
                socket.send_json({"type": "interrupt"})
                return say("yes")
            return say("yes", reply_to=reply["id"])

        say(f"Can you book a meeting on {friday:%A %B} {friday.day} at 9:01 a.m.?")
        say("Sasha Mendez")
        options = confirm(say("17 April 1994"))
        assert "I don't have a 9:01 AM opening" in options["text"]
        assert len(options["calendar"]) == 3
        assert "What email address" in say("option one is fine")["text"]
        proposal = confirm(say("sasha@example.test"))
        assert "09:00 AM" in proposal["text"]
        assert confirm(proposal)["outcome"] == "booked"

    (appointment,) = asyncio.run(DemoStore(database).search("Appointment"))
    start = instant(appointment["start"]).astimezone(BAYVIEW.timezone)
    assert (start.date(), start.hour, start.minute) == (friday, 9, 0)


def test_browser_can_correct_misheard_sample_name_without_repeating_dob(tmp_path, monkeypatch):
    database = tmp_path / "browser.sqlite"
    monkeypatch.setenv("SWITCHBOARD_DB", str(database))
    with (
        TestClient(app) as client,
        client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket,
    ):
        socket.receive_json()
        socket.receive_json()

        def say(text):
            socket.send_json({"type": "text", "text": text})
            return socket.receive_json()

        def played(reply):
            assert reply["confirmation"]
            socket.send_json({"type": "played", "id": reply["id"]})

        say("I want to book a visit")
        say("it's Mario Lopez")
        wrong = say("July 2, 1984")
        assert "Mario Lopez, born July 2, 1984" in wrong["text"]
        played(wrong)
        assert "Which should I change" in say("no")["text"]
        corrected = say("My name is Maria Lopez")
        assert "Maria Lopez, born July 2, 1984" in corrected["text"]
        played(corrected)
        assert "Option 1" in say("correct")["text"]
        appointment = say("option one")
        assert "Book your" in appointment["text"]
        played(appointment)
        assert say("yes")["outcome"] == "booked"
        (saved,) = asyncio.run(
            DemoStore(database).search("Appointment", patient="Patient/maria-demo")
        )
        assert saved["status"] == "booked"


@pytest.mark.parametrize(
    "spoken",
    [
        "16th August 2001",
        "It's 2001 August 16th.",
        "I was born on the 16th of Aug, 2001.",
        "August the 16th, 2001",
        "2001 0816",
    ],
)
def test_spoken_dates(spoken):
    assert parse_date(spoken) == date(2001, 8, 16)
    assert parse_date("30th February 2001") is None


def test_browser_books_an_unseeded_patient_with_explicit_confirmation(tmp_path, monkeypatch):
    database = tmp_path / "browser.sqlite"
    monkeypatch.setenv("SWITCHBOARD_DB", str(database))
    with (
        TestClient(app) as client,
        client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket,
    ):
        socket.receive_json()
        socket.receive_json()
        store = DemoStore(database)

        def say(text):
            socket.send_json({"type": "text", "text": text})
            return socket.receive_json()

        def confirm(reply):
            assert reply["confirmation"]
            socket.send_json({"type": "played", "id": reply["id"]})
            return say("Yes, that's right")

        unavailable = say("Can I book next week with Doctor Reyes?")
        assert "Dr. Reyes isn't on the schedule" in unavailable["text"]
        assert "name" in say("yes")["text"]
        assert "date of birth" in say("My full name is Jamie O'Neill")["text"]
        assert "what year" in say("23rd of September")["text"].lower()
        dob = say("1993")
        assert "September 23, 1993" in dob["text"]
        assert "Option 1" in confirm(dob)["text"]
        assert "What email address" in say("option one")["text"]
        proposed = confirm(say("jamie@example.test"))
        assert "Create a new demo patient record" in proposed["text"]
        repeated = say("yes")
        assert repeated["confirmation"]
        assert not asyncio.run(store.search("Patient", family="O'Neill"))
        assert confirm(repeated)["outcome"] == "booked"
        (patient,) = asyncio.run(store.search("Patient", family="O'Neill"))
        (appointment,) = asyncio.run(
            store.search("Appointment", patient=f"Patient/{patient['id']}")
        )
        assert patient["birthDate"] == "1993-09-23"
        assert appointment["status"] == "booked"


@pytest.mark.parametrize(
    ("name", "returning_name"),
    [
        ("José M. de la Cruz", "JOSE\u0301 M DE LA CRUZ"),
        ("सावित्री", "सावित्री"),
        ("Élodie O’Connor", "Élodie O'Connor"),
    ],
)
def test_browser_books_and_finds_unseeded_names(tmp_path, monkeypatch, name, returning_name):
    database = tmp_path / "browser.sqlite"
    monkeypatch.setenv("SWITCHBOARD_DB", str(database))
    with TestClient(app) as client:
        for intent, patient_name, outcome in (
            ("book next week", name, "booked"),
            ("cancel my appointment", returning_name, "cancelled"),
        ):
            with client.websocket_connect(
                "/call", headers={"origin": "http://testserver"}
            ) as socket:
                socket.receive_json()
                socket.receive_json()

                def say(text):
                    socket.send_json({"type": "text", "text": text})
                    return socket.receive_json()

                def confirm(reply):
                    assert reply["confirmation"]
                    socket.send_json({"type": "played", "id": reply["id"]})
                    return say("yes")

                say(intent)
                assert "date of birth" in say(f"My name is {patient_name}")["text"]
                assert "Option 1" in confirm(say("14th May 1992"))["text"]
                proposal = say("option one")
                if outcome == "booked":
                    assert "What email address" in proposal["text"]
                    proposal = confirm(say("patient@example.test"))
                assert confirm(proposal)["outcome"] == outcome

    store = DemoStore(database)
    (patient,) = asyncio.run(store.search("Patient", birthdate="1992-05-14"))
    (appointment,) = asyncio.run(store.search("Appointment", patient=f"Patient/{patient['id']}"))
    assert patient["name"][0]["text"] == name
    assert appointment["status"] == "cancelled"


def test_browser_rejects_cross_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "browser.sqlite"))
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/call", headers={"origin": "https://example.test"}),
    ):
        pytest.fail("Unexpected cross-origin connection")


def test_operations_is_token_gated_and_omits_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "browser.sqlite"))
    token = "synthetic-operations-token-test"
    monkeypatch.setenv("SWITCHBOARD_OPERATIONS_TOKEN", token)
    with TestClient(app) as client:
        assert client.get("/operations").status_code == 200
        assert client.get("/api/operations").status_code == 401
        with client.websocket_connect("/call", headers={"origin": "http://testserver"}) as socket:
            socket.receive_json()
            socket.receive_json()
            for text in ["book", "Maria Lopez", "1984-07-02"]:
                socket.send_json({"type": "text", "text": text})
                socket.receive_json()
            trace = client.get("/api/operations", headers={"Authorization": f"Bearer {token}"})
            assert trace.status_code == 200
            assert len(trace.json()["live"][0]["events"]) == 4
            assert "Maria Lopez" not in trace.text
            assert "1984-07-02" not in trace.text
