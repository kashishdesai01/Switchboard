"""Run synthetic calls through the browser WebSocket and a running local Ollama model."""

import json
import os
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from fastapi.testclient import TestClient

from switchboard.api import app
from switchboard.scheduling import instant


def booking(say):
    opening = say("Could I see Dr. Patel the coming week? My shoulder has been sore.")
    assert "for you or for someone else" in opening["text"]
    assert "name" in say("For me.")["text"]
    say("Maria Lopez")
    identity = say("zero seven zero two eight four")
    assert "July 2, 1984" in identity["text"]
    options = say("Yep, that's me.", identity)
    assert len(options["calendar"]) == 2 and "Option 3" not in options["text"]
    assert (
        "Dr." not in options["text"]
        and ":" not in options["text"].split("Option 1:")[1].split("Option 2:")[0]
    )
    revised = say("Could you look near lunchtime instead?")
    assert len(revised["calendar"]) == 2
    assert revised["calendar"] != options["calendar"]
    window = app.state.scheduling.now().astimezone(app.state.scheduling.clinic.timezone)
    start_days = [
        instant(option["start"]).astimezone(window.tzinfo).date() for option in revised["calendar"]
    ]
    assert all(
        7 - window.weekday() <= (day - window.date()).days < 14 - window.weekday()
        for day in start_days
    )
    preferred = max(instant(option["start"]) for option in revised["calendar"])
    held = say("I'll take the latest one, please.")
    assert held["confirmation"]
    assert preferred == instant(held["calendar"][0]["start"])
    repeated = say("Yes")
    assert repeated["outcome"] is None and repeated["confirmation"]
    assert say("Yes, go ahead", repeated)["outcome"] == "booked"
    return preferred


def emergency(say):
    assert (
        say("Dad had a stroke several years ago. Could I come in to see Dr. Garcia?")["outcome"]
        is None
    )
    assert (
        say("My brother's speech sounds garbled, and his mouth is drooping.")["outcome"]
        == "emergency"
    )


def staff(say):
    assert (
        say("My checkup starts in ten minutes and I'm going to be a little late.")["outcome"]
        == "handoff"
    )


def run_scenario(check, clinic, directory):
    os.environ["SWITCHBOARD_DB"] = str(directory / f"{check.__name__}.sqlite")
    os.environ["SWITCHBOARD_OPERATIONS_DB"] = str(directory / f"{check.__name__}.ops.sqlite")
    os.environ["SWITCHBOARD_CLINIC_FILE"] = str(Path("clinics") / f"{clinic}.toml")
    timings = []
    with TestClient(app) as client:
        with client.websocket_connect(
            "/call?spoken=1", headers={"origin": "http://testserver"}
        ) as socket:
            assert socket.receive_json()["type"] == "ready"
            assert socket.receive_json()["type"] == "reply"

            def say(text, heard=None):
                began = perf_counter()
                socket.send_json({"type": "text", "text": text, "reply_to": heard and heard["id"]})
                reply = socket.receive_json()
                timings.append(round((perf_counter() - began) * 1000))
                return reply

            preferred = check(say)
        records = client.portal.call(app.state.scheduling.appointments, "maria-demo")
        if preferred is not None:
            assert len(records) == 1 and preferred == instant(records[0]["start"])
        else:
            assert records == []
        # A result based entirely on the deterministic fallback is insufficient.
        trace = client.portal.call(app.state.ops.snapshot)["recent"][0]
        assert any(event["event"] == "context_interpreted" for event in trace["events"])
    return {"scenario": check.__name__, "clinic": clinic, "turns_ms": timings}


def main():
    os.environ.setdefault("SWITCHBOARD_OLLAMA_MODEL", "qwen2.5:3b")
    os.environ.pop("SWITCHBOARD_FHIR_URL", None)
    with TemporaryDirectory() as directory:
        report = [
            run_scenario(check, clinic, Path(directory))
            for check, clinic in ((booking, "bayview"), (emergency, "lakeside"), (staff, "bayview"))
        ]
    timings = [value for scenario in report for value in scenario["turns_ms"]]
    print(json.dumps({"scenarios": report, "median_text_turn_ms": median(timings)}, indent=2))


if __name__ == "__main__":
    main()
