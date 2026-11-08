from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from switchboard.api import app
from switchboard.telephony import StreamTickets

ORIGIN = "https://demo.example"
TOKEN = "not-a-real-twilio-token"
CALLER = "+14155550101"
ACCOUNT = "AC" + "a" * 32
CALL = "CA" + "b" * 32


@pytest.fixture
def client(monkeypatch, tmp_path):
    async def no_filler(_app, _http):
        pass

    monkeypatch.setattr("switchboard.api.warm_filler", no_filler)
    for key, value in {
        "SWITCHBOARD_PUBLIC_ORIGIN": ORIGIN,
        "TWILIO_AUTH_TOKEN": TOKEN,
        "TWILIO_ACCOUNT_SID": ACCOUNT,
        "DEEPGRAM_API_KEY": "unused",
        "SWITCHBOARD_DEMO_CALLERS": CALLER,
        "SWITCHBOARD_DEMO_STAFF": "+14155550102",
        "SWITCHBOARD_DB": str(tmp_path / "app.sqlite"),
        "SWITCHBOARD_OPERATIONS_TOKEN": "synthetic-replay-operations-token",
        "SWITCHBOARD_TELEPHONY_DB": str(tmp_path / "telephone.sqlite"),
    }.items():
        monkeypatch.setenv(key, value)
    with TestClient(app) as client:
        yield client


def signed_post(client, path, **fields):
    data = {"AccountSid": ACCOUNT, "CallSid": CALL, **fields}
    signature = RequestValidator(TOKEN).compute_signature(ORIGIN + path, data)
    return client.post(path, data=data, headers={"X-Twilio-Signature": signature})


def test_signed_entry_binds_ticket_to_call_and_rejects_replay(client):
    assert client.post("/twilio/voice", data={"CallSid": CALL}).status_code == 403
    blocked = signed_post(client, "/twilio/voice", From="+14155550105")
    assert fromstring(blocked.text).find("Reject") is not None
    allowed = signed_post(client, "/twilio/voice", From=CALLER)
    root = fromstring(allowed.text)
    token = root.find("Connect/Stream/Parameter").attrib["value"]
    assert (
        fromstring(signed_post(client, "/twilio/voice", From=CALLER).text)
        .find("Connect/Stream/Parameter")
        .attrib["value"]
        == token
    )
    tickets = app.state.tickets
    assert not tickets.consume("CA" + "c" * 32, token)
    assert tickets.consume(CALL, token)
    assert not tickets.consume(CALL, token)


def test_transfer_failure_and_emergency_routing(client):
    signed_post(client, "/twilio/voice", From=CALLER)
    tickets = app.state.tickets
    tickets.finish(CALL, "handoff")
    result = signed_post(client, "/twilio/after-stream")
    assert fromstring(result.text).find("Dial/Number").text == "+14155550102"
    no_answer = signed_post(client, "/twilio/dial-result", DialCallStatus="no-answer")
    assert "Staff are unavailable" in no_answer.text
    tickets.finish(CALL, "emergency")
    assert fromstring(signed_post(client, "/twilio/after-stream").text).find("Dial") is None
    tickets.finish(CALL, "emergency_unplayed")
    fallback = signed_post(client, "/twilio/after-stream")
    assert "call 911 now" in fallback.text
    assert fromstring(fallback.text).find("Dial") is None


def test_ticket_expires(tmp_path, monkeypatch):
    now = 1000
    monkeypatch.setattr("switchboard.telephony.time", lambda: now)
    tickets = StreamTickets(Path(tmp_path) / "tickets.sqlite", TOKEN)
    ticket = tickets.mint(CALL)
    now += 61
    assert not tickets.consume(CALL, ticket)


def test_signed_media_websocket_replays_a_booking(client, monkeypatch):
    import asyncio
    import base64
    import json
    from contextlib import asynccontextmanager

    class SpeechConnection:
        def __init__(self):
            self.results = asyncio.Queue()

        async def send(self, audio):
            text = audio.rstrip(b"\xff").decode()
            await self.results.put(json.dumps({"type": "SpeechStarted"}))
            await self.results.put(
                json.dumps(
                    {
                        "type": "Results",
                        "is_final": True,
                        "speech_final": True,
                        "channel": {"alternatives": [{"transcript": text}]},
                    }
                )
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.results.get()

    class ReplaySpeech:
        def __init__(self, *_args):
            pass

        @asynccontextmanager
        async def listen(self):
            yield SpeechConnection()

        async def synthesize(self, _text):
            yield b"\xff" * 160

    monkeypatch.setattr("switchboard.telephony.Deepgram", ReplaySpeech)
    xml = signed_post(client, "/twilio/voice", From=CALLER).text
    ticket = fromstring(xml).find("Connect/Stream/Parameter").attrib["value"]
    media_signature = RequestValidator(TOKEN).compute_signature(
        "wss://demo.example/twilio/media", {}
    )
    stream = "MZ" + "d" * 32
    with client.websocket_connect(
        "/twilio/media", headers={"X-Twilio-Signature": media_signature}
    ) as socket:
        socket.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        socket.send_json(
            {
                "event": "start",
                "streamSid": stream,
                "start": {
                    "accountSid": ACCOUNT,
                    "callSid": CALL,
                    "streamSid": stream,
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                    "customParameters": {"ticket": ticket},
                },
            }
        )

        def play():
            assert socket.receive_json()["event"] == "media"
            mark = socket.receive_json()
            assert mark["event"] == "mark"
            socket.send_json(mark)

        play()
        for utterance in ["book", "Maria Lopez", "1984-07-02", "yes", "one", "yes"]:
            socket.send_json(
                {
                    "event": "media",
                    "streamSid": stream,
                    "media": {
                        "payload": base64.b64encode(utterance.encode().ljust(160, b"\xff")).decode()
                    },
                }
            )
            play()
        assert socket.receive()["type"] == "websocket.close"
    assert app.state.tickets.outcome(CALL) == "booked"
    assert len(asyncio.run(app.state.scheduling.appointments("maria-demo"))) == 1


@pytest.mark.parametrize("name", ["booking", "emergency-barge-in"])
def test_paced_replay_uses_signed_transport_and_journal(client, monkeypatch, name):
    import asyncio
    import json
    from contextlib import asynccontextmanager

    import httpx

    from evals.replay_audio import SCENARIOS, SILENCE, replay_twilio

    scenario = SCENARIOS[name]
    clips = [bytes([index + 1]) * 160 for index in range(len(scenario.lines))]

    class ReplaySpeech:
        def __init__(self, *_args):
            self.results = asyncio.Queue()
            self.buffer = bytearray()

        @asynccontextmanager
        async def listen(self):
            yield self

        async def send(self, data):
            self.buffer.extend(data)
            while len(self.buffer) >= 160:
                frame = bytes(self.buffer[:160])
                del self.buffer[:160]
                if frame == SILENCE:
                    continue
                line = scenario.lines[frame[0] - 1]
                await self.results.put(
                    json.dumps(
                        {
                            "type": "Results",
                            "is_final": True,
                            "speech_final": True,
                            "channel": {"alternatives": [{"transcript": line}]},
                        }
                    )
                )

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.results.get()

        async def synthesize(self, _text):
            yield b"\xff" * (8000 if scenario.barge_in else 160)

    class Socket:
        def __init__(self, socket):
            self.socket = socket

        async def send(self, value):
            await asyncio.to_thread(self.socket.send_text, value)

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await asyncio.to_thread(self.socket.receive)
            if item["type"] == "websocket.close":
                raise StopAsyncIteration
            return item["text"]

    @asynccontextmanager
    async def open_socket(url, *, additional_headers):
        assert url == "ws://127.0.0.1/twilio/media"
        with client.websocket_connect("/twilio/media", headers=additional_headers) as socket:
            yield Socket(socket)

    def request_to_app(request):
        result = client.request(
            request.method, request.url.path, headers=request.headers, content=request.content
        )
        return httpx.Response(result.status_code, headers=result.headers, content=result.content)

    monkeypatch.setattr("switchboard.telephony.Deepgram", ReplaySpeech)
    monkeypatch.setattr("evals.replay_audio.connect", open_socket)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(request_to_app)) as http:
            return await replay_twilio(
                http,
                "http://127.0.0.1",
                ORIGIN,
                ACCOUNT,
                TOKEN,
                CALLER,
                "synthetic-replay-operations-token",
                clips,
                barge_in=scenario.barge_in,
            )

    result = asyncio.run(run())
    assert result["outcome"] == scenario.outcome
    assert result["media_outcome"] == scenario.outcome
    assert result["committed_writes"] == int(name == "booking")
    if scenario.barge_in:
        assert result["clear_events"] == 1
        assert result["barge_in_to_clear_ms"] is not None
    else:
        assert result["measured_turns"] == len(clips)
        assert result["clear_events"] == 0
