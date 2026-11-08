"""Replay synthetic μ-law audio against the browser or Twilio media adapter."""

import argparse
import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from math import ceil
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import httpx
from twilio.request_validator import RequestValidator
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK

from switchboard.voice import Deepgram

FRAME_BYTES = 160
FRAME_SECONDS = 0.02
SILENCE = b"\xff" * FRAME_BYTES


@dataclass(frozen=True)
class Scenario:
    lines: tuple[str, ...]
    outcome: str
    barge_in: bool = False


IDENTITY = ("Maria Lopez", "July 2, 1984", "yes")
BOOKING = ("I want to book a follow up next week", *IDENTITY, "option one")
SCENARIOS = {
    "booking": Scenario((*BOOKING, "yes"), "booked"),
    "declined-booking": Scenario((*BOOKING, "no"), "declined"),
    "coverage": Scenario(("Check my coverage", *IDENTITY), "info_only"),
    "staff-handoff": Scenario(("I need to talk to a human.",), "handoff"),
    "emergency": Scenario(("I can't breathe. I have chest pain.",), "emergency"),
    "emergency-barge-in": Scenario(("I can't breathe. I have chest pain.",), "emergency", True),
}


async def replay_stream(
    packets: AsyncIterator[bytes | dict],
    send_frame: Callable[[bytes], Awaitable[None]],
    send_mark: Callable[[str], Awaitable[None]],
    clips: list[bytes],
    *,
    barge_in: bool = False,
    echo_cleared_marks: bool = False,
) -> dict:
    """Pace caller frames and acknowledge agent audio only after simulated playback."""
    frames: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue()
    clips_left = iter(clips)
    latencies: list[int] = []
    last_spoken = first_audio = barge_start = None
    playback_end = 0.0
    played = 0
    clears = 0
    barge_reaction_ms = None
    pending_marks: dict[str, asyncio.Task] = {}
    acknowledgements: list[asyncio.Task] = []
    outcome = None

    def enqueue_next_clip(*, pause: bool = False) -> None:
        clip = next(clips_left, None)
        if clip:
            if pause:
                for _ in range(13):
                    frames.put_nowait((SILENCE, False))
            for offset in range(0, len(clip), FRAME_BYTES):
                chunk = clip[offset : offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\xff")
                frames.put_nowait((chunk, True))

    async def pace() -> None:
        nonlocal last_spoken, barge_start
        deadline = monotonic()
        while True:
            frame, speaking = frames.get_nowait() if not frames.empty() else (SILENCE, False)
            started = monotonic()
            if speaking and barge_in and barge_start is None:
                barge_start = started
            await send_frame(frame)
            if speaking and frames.empty():
                last_spoken = started
            # Skip missed deadlines rather than sending a burst after a stall.
            deadline = max(deadline + FRAME_SECONDS, monotonic())
            await asyncio.sleep(max(0, deadline - monotonic()))

    async def acknowledge(mark: str, deadline: float, *, filler: bool) -> None:
        nonlocal played
        await asyncio.sleep(max(0, deadline - monotonic()))
        pending_marks.pop(mark, None)
        await send_mark(mark)
        if not filler:
            played += 1
            if not barge_in:
                enqueue_next_clip(pause=True)

    async def clear() -> None:
        marks = list(pending_marks)
        tasks = list(pending_marks.values())
        pending_marks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if echo_cleared_marks:
            for mark in marks:
                await send_mark(mark)

    sender = asyncio.create_task(pace())
    try:
        async with asyncio.timeout(90):
            async for packet in packets:
                if sender.done():
                    sender.result()
                if isinstance(packet, bytes):
                    received = monotonic()
                    playback_end = max(playback_end, received) + len(packet) / 8000
                    if first_audio is None:
                        first_audio = received
                    if barge_in and barge_start is None and frames.empty():
                        enqueue_next_clip()
                    continue
                match packet["type"]:
                    case "mark":
                        mark = packet["id"]
                        filler = mark.startswith("filler-")
                        if not filler and last_spoken is not None and first_audio is not None:
                            if first_audio >= last_spoken:
                                latencies.append(round((first_audio - last_spoken) * 1000))
                            last_spoken = None
                        first_audio = None
                        task = asyncio.create_task(acknowledge(mark, playback_end, filler=filler))
                        pending_marks[mark] = task
                        acknowledgements.append(task)
                    case "clear":
                        clears += 1
                        if barge_start is not None and barge_reaction_ms is None:
                            barge_reaction_ms = round((monotonic() - barge_start) * 1000)
                        playback_end = 0
                        first_audio = None
                        await clear()
                    case "ended":
                        outcome = packet["outcome"]
                        break
    finally:
        tasks = [sender, *acknowledgements]
        for task in tasks:
            task.cancel()
        finished = await asyncio.gather(*tasks, return_exceptions=True)
        for error in finished:
            if isinstance(error, Exception) and not isinstance(error, ConnectionClosedOK):
                raise error

    ordered = sorted(latencies)

    def percentile(fraction: float) -> int | None:
        return ordered[ceil(len(ordered) * fraction) - 1] if ordered else None

    return {
        "outcome": outcome,
        "played_responses": played,
        "measured_turns": len(ordered),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "clear_events": clears,
        "barge_in_to_clear_ms": barge_reaction_ms,
    }


async def browser_packets(socket: ClientConnection) -> AsyncIterator[bytes | dict]:
    async for packet in socket:
        yield packet if isinstance(packet, bytes) else json.loads(packet)


async def replay(
    url: str, token: str | None, clips: list[bytes], *, barge_in: bool = False
) -> dict:
    address = urlsplit(url)
    origin = f"{'https' if address.scheme == 'wss' else 'http'}://{address.netloc}"
    async with connect(url, origin=origin) as socket:
        if token:
            await socket.send(token)

        async def mark(name: str) -> None:
            await socket.send(json.dumps({"type": "mark", "id": name}))

        result = await replay_stream(
            browser_packets(socket), socket.send, mark, clips, barge_in=barge_in
        )
    if result["outcome"] is None:
        raise RuntimeError("Browser stream closed without a terminal outcome")
    return result


class TwilioStream:
    """Send the Media Streams messages a signed TwiML request would lead to."""

    def __init__(self, socket: ClientConnection, account: str, call_sid: str, stream_sid: str):
        self.socket = socket
        self.account = account
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.sequence = 0
        self.chunk = 0

    async def send(self, event: str, **fields) -> None:
        self.sequence += 1
        await self.socket.send(
            json.dumps(
                {
                    "event": event,
                    "sequenceNumber": str(self.sequence),
                    "streamSid": self.stream_sid,
                    **fields,
                }
            )
        )

    async def start(self, ticket: str) -> None:
        await self.socket.send(
            json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        )
        await self.send(
            "start",
            start={
                "accountSid": self.account,
                "callSid": self.call_sid,
                "streamSid": self.stream_sid,
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"ticket": ticket},
            },
        )

    async def frame(self, frame: bytes) -> None:
        self.chunk += 1
        await self.send(
            "media",
            media={
                "track": "inbound",
                "chunk": str(self.chunk),
                "timestamp": str((self.chunk - 1) * 20),
                "payload": base64.b64encode(frame).decode(),
            },
        )

    async def mark(self, name: str) -> None:
        await self.send("mark", mark={"name": name})

    async def packets(self) -> AsyncIterator[bytes | dict]:
        async for raw in self.socket:
            packet = json.loads(raw)
            if packet["streamSid"] != self.stream_sid:
                raise RuntimeError("Unexpected stream ID in replay")
            match packet["event"]:
                case "media":
                    yield base64.b64decode(packet["media"]["payload"], validate=True)
                case "mark":
                    yield {"type": "mark", "id": packet["mark"]["name"]}
                case "clear":
                    yield {"type": "clear"}


async def replay_twilio(
    client: httpx.AsyncClient,
    local_origin: str,
    public_origin: str,
    account: str,
    auth_token: str,
    caller: str,
    ops_token: str,
    clips: list[bytes],
    *,
    barge_in: bool = False,
) -> dict:
    local = urlsplit(local_origin)
    if local.scheme != "http" or local.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Twilio replay requires a local synthetic server")
    public_origin = public_origin.rstrip("/")
    local_origin = local_origin.rstrip("/")
    validator = RequestValidator(auth_token)
    call_sid = "CA" + uuid4().hex
    data = {"AccountSid": account, "CallSid": call_sid, "From": caller}
    auth = {"Authorization": f"Bearer {ops_token}"}

    async def operations() -> dict:
        response = await client.get(f"{local_origin}/api/operations", headers=auth)
        response.raise_for_status()
        return response.json()

    existing = {item["id"] for item in (await operations())["recent"]}
    response = await client.post(
        f"{local_origin}/twilio/voice",
        data=data,
        headers={
            "X-Twilio-Signature": validator.compute_signature(f"{public_origin}/twilio/voice", data)
        },
    )
    response.raise_for_status()
    root = fromstring(response.text)
    stream_config = root.find("./Connect/Stream")
    parameter = root.find("./Connect/Stream/Parameter[@name='ticket']")
    if stream_config is None or parameter is None:
        raise RuntimeError(
            "Signed webhook did not grant a stream ticket. Check the demo allowlist."
        )

    signed_url = f"wss://{urlsplit(public_origin).netloc}/twilio/media"
    if stream_config.get("url") != signed_url:
        raise RuntimeError("TwiML did not advertise the expected media socket")
    signature = validator.compute_signature(signed_url, {})
    async with connect(
        f"ws://{local.netloc}/twilio/media",
        additional_headers={"X-Twilio-Signature": signature},
    ) as socket:
        stream = TwilioStream(socket, account, call_sid, "MZ" + uuid4().hex)
        await stream.start(parameter.attrib["value"])
        result = await replay_stream(
            stream.packets(),
            stream.frame,
            stream.mark,
            clips,
            barge_in=barge_in,
            echo_cleared_marks=True,
        )

    snapshot = await operations()
    matching = [
        item
        for item in snapshot["recent"]
        if item["transport"] == "twilio" and item["id"] not in existing
    ]
    if len(matching) != 1:
        raise RuntimeError("Replay requires an isolated instance with one active Twilio test")
    trace = matching[0]
    result["outcome"] = trace["outcome"]
    result["media_outcome"] = next(
        (
            item["event"].removeprefix("media_")
            for item in reversed(trace["events"])
            if item["event"].startswith("media_")
        ),
        None,
    )
    result["committed_writes"] = sum(
        item["trace_id"] == trace["id"] and item["status"] == "committed"
        for item in snapshot["settled_mutations"]
    )
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("browser", "twilio"), default="browser")
    parser.add_argument("--scenario", choices=SCENARIOS, default="booking")
    parser.add_argument("--caller-voice", default="aura-2-thalia-en")
    parser.add_argument("--max-barge-ms", type=int)
    args = parser.parse_args()
    if args.max_barge_ms is not None and (
        args.max_barge_ms <= 0 or not SCENARIOS[args.scenario].barge_in
    ):
        parser.error("--max-barge-ms requires a positive number and a barge-in scenario")
    scenario = SCENARIOS[args.scenario]
    if args.transport == "twilio":
        twilio_target = (
            os.getenv("SWITCHBOARD_DEMO_URL", "http://127.0.0.1:8765"),
            os.environ["SWITCHBOARD_PUBLIC_ORIGIN"],
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
            os.environ["SWITCHBOARD_REPLAY_CALLER"],
            os.environ["SWITCHBOARD_OPERATIONS_TOKEN"],
        )
    async with httpx.AsyncClient(timeout=12) as client:
        if args.transport == "browser":
            browser_url = os.getenv("SWITCHBOARD_AUDIO_WS", "ws://127.0.0.1:8765/voice-lab")
            address = urlsplit(browser_url)
            scheme = "https" if address.scheme == "wss" else "http"
            capabilities = await client.get(f"{scheme}://{address.netloc}/capabilities")
            capabilities.raise_for_status()
            browser_token = (
                os.environ["SWITCHBOARD_BROWSER_AUDIO_TOKEN"]
                if capabilities.json().get("streaming_audio_token_required", True)
                else None
            )
        tts = Deepgram(os.environ["DEEPGRAM_API_KEY"], client, voice=args.caller_voice)
        clips = [
            b"".join([chunk async for chunk in tts.synthesize(line)]) for line in scenario.lines
        ]
        if not all(clips):
            raise RuntimeError("Speech provider returned an empty caller clip")
        if args.transport == "browser":
            result = await replay(
                browser_url,
                browser_token,
                clips,
                barge_in=scenario.barge_in,
            )
        else:
            result = await replay_twilio(
                client,
                *twilio_target,
                clips,
                barge_in=scenario.barge_in,
            )
    result.update(
        transport=args.transport,
        scenario=args.scenario,
        caller_voice=args.caller_voice,
        scope="synthetic audio, final caller frame to first substantive response frame",
    )
    print(json.dumps(result, indent=2))
    success = result["outcome"] == scenario.outcome
    if args.transport == "twilio":
        success &= result["media_outcome"] == scenario.outcome
        success &= result["committed_writes"] == int(scenario.outcome == "booked")
    if scenario.barge_in:
        reaction = result["barge_in_to_clear_ms"]
        success &= reaction is not None
        if args.max_barge_ms is not None:
            success &= reaction is not None and reaction <= args.max_barge_ms
    raise SystemExit(not success)


if __name__ == "__main__":
    asyncio.run(main())
