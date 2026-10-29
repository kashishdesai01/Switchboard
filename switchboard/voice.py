"""Transport-independent playback and streaming speech for the telephone demo."""

import asyncio
import json
import os
import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from switchboard.conversation import (
    EMERGENCY,
    STAFF,
    Call,
    Reply,
    answer_to_confirmation,
    parse_date,
)

SAMPLE_RATE = 8000
REPLAY_SAMPLES = SAMPLE_RATE // 2
TURN_TIMEOUT_SECONDS = 15
PLAYBACK_TIMEOUT_SECONDS = 20
PLAYBACK_MARK_GRACE_SECONDS = 5
FILLER_DELAY_SECONDS = 1.5
FILLER_TEXT = "One moment while I check."


@dataclass(frozen=True)
class Speech:
    kind: str
    text: str = ""
    onset: float | None = None


class Media(Protocol):
    async def receive(
        self, audio: Callable[[bytes], Awaitable[None]], queue: asyncio.Queue[Speech]
    ) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...

    async def mark(self, name: str) -> None: ...

    async def clear(self) -> None: ...


class RecognitionStream(AsyncIterable[str | bytes], Protocol):
    async def send(self, audio: bytes) -> None: ...


class SpeechProvider(Protocol):
    def listen(self) -> AbstractAsyncContextManager[RecognitionStream]: ...

    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...


class Deepgram:
    def __init__(self, api_key: str, client: httpx.AsyncClient, *, voice: str | None = None):
        self.voice = voice or os.getenv("SWITCHBOARD_DEEPGRAM_VOICE", "aura-2-helena-en")
        self.headers = {"Authorization": f"Token {api_key}"}
        self.client = client

    def listen(self):
        params = urlencode(
            {
                "model": "nova-3",
                "language": "en-US",
                "encoding": "mulaw",
                "sample_rate": 8000,
                "channels": 1,
                "interim_results": "true",
                "endpointing": 300,
                "utterance_end_ms": 1000,
                "vad_events": "true",
                "smart_format": "true",
            }
        )
        return connect(
            f"wss://api.deepgram.com/v1/listen?{params}",
            additional_headers=self.headers,
            open_timeout=4,
            close_timeout=1,
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with self.client.stream(
            "POST",
            "https://api.deepgram.com/v1/speak",
            headers={**self.headers, "Content-Type": "application/json"},
            params={
                "model": self.voice,
                "encoding": "mulaw",
                "sample_rate": 8000,
                "container": "none",
            },
            json={"text": text},
        ) as response:
            response.raise_for_status()
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type != "audio/mulaw":
                raise httpx.DecodingError("Expected raw μ-law speech", request=response.request)
            async for chunk in response.aiter_bytes():
                for offset in range(0, len(chunk), 1600):
                    yield chunk[offset : offset + 1600]


async def render_filler(speech: SpeechProvider) -> bytes:
    """Cache one short cue per process, with a bounded startup request."""
    data = bytearray()
    try:
        async with asyncio.timeout(4):
            async for chunk in speech.synthesize(FILLER_TEXT):
                data.extend(chunk)
                if len(data) > 3 * SAMPLE_RATE:
                    return b""
    except (httpx.HTTPError, TimeoutError):
        return b""
    return bytes(data)


class AudioOverrun(Exception):
    """Recognition fell behind the bounded caller audio buffer."""


class CallerAudio:
    """A 500 ms ring with positions in the original caller audio stream."""

    def __init__(self):
        self.end = 0
        self.buffer = bytearray()
        self.changed = asyncio.Event()

    async def append(self, data: bytes) -> None:
        self.end += len(data)
        self.buffer.extend(data)
        del self.buffer[:-REPLAY_SAMPLES]
        self.changed.set()

    @property
    def start(self) -> int:
        return self.end - len(self.buffer)

    async def forward(
        self, send: Callable[[bytes], Awaitable[None]], cursor: int, replay: bytes
    ) -> None:
        if replay:
            await send(replay)
        while True:
            while cursor == self.end:
                self.changed.clear()
                await self.changed.wait()
            if cursor < self.start:
                raise AudioOverrun
            # Snapshot position and data together before yielding to the socket.
            end = self.end
            await send(bytes(self.buffer[cursor - self.start :]))
            cursor = end


async def speech_events(
    upstream: AsyncIterable[str | bytes],
    queue: asyncio.Queue[Speech],
    *,
    awaiting_date: Callable[[], bool] = lambda: False,
    offset: float = 0,
    replay_seconds: float = 0,
) -> None:
    segments: list[str] = []
    first_word: float | None = None

    async def finish() -> None:
        nonlocal first_word
        onset = first_word + offset if first_word is not None else None
        await queue.put(Speech("final", " ".join(segments), onset))
        segments.clear()
        first_word = None

    async for raw in upstream:
        packet = json.loads(raw)
        match packet.get("type"):
            case "SpeechStarted":
                onset = packet.get("timestamp")
                if replay_seconds and (onset is None or onset < replay_seconds):
                    continue
                await queue.put(
                    Speech("start", onset=onset + offset if onset is not None else None)
                )
            case "UtteranceEnd":
                if segments:
                    await finish()
            case "Results":
                alternative = packet["channel"]["alternatives"][0]
                text = alternative["transcript"].strip()
                words = alternative.get("words", [])
                onset = words[0]["start"] if words else packet.get("start")
                if replay_seconds and text and (onset is None or onset < replay_seconds):
                    continue
                if packet.get("is_final") and text:
                    if not segments:
                        first_word = onset
                    segments.append(text)
                elif text:
                    await queue.put(Speech("interim", text))
                if packet.get("speech_final") and segments:
                    combined = " ".join(segments)
                    unfinished = re.search(
                        r"\b(?:um|uh|and|on|at|is)$", combined.rstrip(".!? "), re.I
                    ) or (awaiting_date() and parse_date(combined) is None)
                    if not unfinished or EMERGENCY.search(combined) or STAFF.search(combined):
                        await finish()


def interrupts(text: str) -> bool:
    normalized = text.lower().strip(".!? ")
    if normalized in {"mm-hm", "uh-huh", "okay", "yes", "yeah", "right", "correct"}:
        return False
    return (
        normalized in {"stop", "wait", "no"}
        or bool(EMERGENCY.search(normalized) or STAFF.search(normalized))
        or len(normalized.split()) >= 2
    )


class VoiceCall:
    def __init__(
        self,
        call: Call,
        media: Media,
        speech: SpeechProvider,
        *,
        filler_audio: bytes = b"",
        on_reply: Callable[[Reply], Awaitable[None]] | None = None,
        on_caller: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.on_reply = on_reply
        self.on_caller = on_caller
        self.call = call
        self.media = media
        self.speech = speech
        self.events: asyncio.Queue[Speech] = asyncio.Queue(maxsize=48)
        self.playback: asyncio.Task | None = None
        self.reply: Reply | None = None
        self.overlap = False
        self.audio = CallerAudio()
        self.playback_end_sample: int | None = None
        self.onset_sample: int | None = None
        self.last_final: float | None = None
        self.turn: asyncio.Task[Reply | None] | None = None
        self.recognition_available = True
        self.recognition_exhausted = False
        self.deferred_reply: Reply | None = None
        self.filler_audio = filler_audio
        self.filler_due: float | None = None
        self.filler_mark: str | None = None
        self.playback_deadline = 0.0

    async def _speak(self, reply: Reply) -> None:
        produced_audio = False
        expected_end = 0.0
        failure = "empty_audio"
        synthesis_timeout = max(12, min(55, len(reply.text) / 10 + 8))
        for attempt in range(2):
            try:
                async with asyncio.timeout(synthesis_timeout):
                    async for data in self.speech.synthesize(reply.text):
                        await self.media.send_audio(data)
                        expected_end = max(expected_end, monotonic()) + len(data) / SAMPLE_RATE
                        self.playback_deadline = max(
                            self.playback_deadline, expected_end + PLAYBACK_MARK_GRACE_SECONDS
                        )
                        if not produced_audio and self.last_final is not None and self.call.trace:
                            self.call.trace.add(
                                "final_transcript_to_audio",
                                latency_ms=round((monotonic() - self.last_final) * 1000),
                            )
                        produced_audio = True
                    if produced_audio:
                        await self.media.mark(reply.id)
                        return
            except (OSError, RuntimeError, httpx.HTTPError, TimeoutError) as exc:
                failure = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    failure = f"http_{status}"
                    if status != 429 and status < 500:
                        break
            if produced_audio:
                break
            if attempt == 0:
                await asyncio.sleep(0.4)
        if self.call.trace:
            self.call.trace.add("speech_playback_failed", state=failure)
        await self.events.put(Speech("failure"))

    async def _play(self, reply: Reply) -> None:
        self.reply = reply
        self.playback_deadline = monotonic() + PLAYBACK_TIMEOUT_SECONDS
        self.playback_end_sample = None
        if self.on_reply:
            await self.on_reply(reply)
        self.playback = asyncio.create_task(self._speak(reply))

    async def _stop(self, *, clear: bool = True) -> None:
        self.reply = None
        self.call.interrupt()
        if self.playback:
            self.playback.cancel()
            with suppress(asyncio.CancelledError):
                await self.playback
            self.playback = None
        if clear:
            await self.media.clear()
            if self.call.trace:
                self.call.trace.add("interrupted")

    async def _pump(self, source: Awaitable[None], end: str | None = None) -> None:
        try:
            await source
        except Exception:
            # Vendor and transport errors become a single failure event. Provider
            # responses can contain caller text and must stay out of logs.
            await self.events.put(Speech("failure"))
        else:
            if end:
                await self.events.put(Speech(end))

    async def _recognize(self) -> None:
        for attempt in range(2):
            try:
                async with self.speech.listen() as upstream:
                    replay = bytes(self.audio.buffer)
                    cursor = self.audio.end
                    offset = (cursor - len(replay)) / SAMPLE_RATE
                    replay_seconds = len(replay) / SAMPLE_RATE if attempt else 0
                    if attempt:
                        await self.events.put(Speech("speech_restored"))
                    tasks = [
                        asyncio.create_task(self.audio.forward(upstream.send, cursor, replay)),
                        asyncio.create_task(
                            speech_events(
                                upstream,
                                self.events,
                                awaiting_date=lambda: self.call.stage == "dob",
                                offset=offset,
                                replay_seconds=replay_seconds,
                            )
                        ),
                    ]
                    try:
                        completed, _ = await asyncio.wait(
                            tasks, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in completed:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            except (OSError, WebSocketException, TimeoutError, AudioOverrun):
                pass
            if not attempt:
                await self.events.put(Speech("speech_lost"))
                await asyncio.sleep(0.2)
        await self.events.put(Speech("speech_unavailable"))

    async def _process_turn(self, event: Speech) -> Reply | None:
        try:
            try:
                async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                    if event.kind == "digit":
                        reply = await self.call.digit(event.text)
                    else:
                        if self.on_caller:
                            await self.on_caller(event.text)
                        reply = await self.call.handle(event.text)
            except TimeoutError:
                await self.call.close()
                reply = self.call.say(
                    "I couldn't finish checking the clinic system. Please check with the front desk.",
                    outcome="needs_review",
                )
        except Exception:
            await self.events.put(Speech("failure"))
            return None
        await self.events.put(Speech("turn_done"))
        return reply

    async def _send_filler(self) -> None:
        self.filler_due = None
        if self.turn and not self.turn.done():
            self.filler_mark = f"filler-{uuid4().hex}"
            for offset in range(0, len(self.filler_audio), 1600):
                await self.media.send_audio(self.filler_audio[offset : offset + 1600])
            await self.media.mark(self.filler_mark)
            if self.call.trace:
                self.call.trace.add("filler_sent")

    async def _clear_filler(self) -> None:
        self.filler_due = None
        if self.filler_mark:
            self.filler_mark = None
            await self.media.clear()

    async def _cancel_turn(self) -> None:
        turn, self.turn = self.turn, None
        if turn:
            turn.cancel()
            with suppress(asyncio.CancelledError):
                await turn

    async def _recognition_handoff(self) -> None:
        await self.call.close()
        await self._play(
            self.call.say(
                "Speech recognition is unavailable. This request needs the front desk.",
                outcome="handoff",
            )
        )

    async def run(self) -> str:
        pumps = [
            asyncio.create_task(
                self._pump(self.media.receive(self.audio.append, self.events), "hangup")
            ),
            asyncio.create_task(self._pump(self._recognize())),
        ]
        try:
            await self._play(self.call.greeting())
            return await self._loop()
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            await self._cancel_turn()
            await self._stop(clear=False)
            await self.call.close()

    async def _loop(self) -> str:
        silence = 0
        while True:
            try:
                if self.turn:
                    async with asyncio.timeout_at(self.filler_due):
                        event = await self.events.get()
                else:
                    event = await asyncio.wait_for(
                        self.events.get(),
                        timeout=max(0, self.playback_deadline - monotonic()) if self.reply else 6,
                    )
            except TimeoutError:
                if self.turn:
                    await self._send_filler()
                    continue
                if self.reply:
                    if monotonic() < self.playback_deadline:
                        continue
                    if self.call.trace:
                        self.call.trace.add("playback_mark_timeout")
                    return "failure"
                silence += 1
                if silence > 2:
                    return "abandoned"
                await self._play(self.call.say("Are you still there?"))
                continue
            if event.kind in {"failure", "hangup"}:
                return event.kind
            if event.kind == "mark" and event.text == self.filler_mark:
                self.filler_mark = None
                continue
            if event.kind == "turn_done":
                if not self.turn:
                    continue
                reply = await self.turn
                self.turn = None
                await self._clear_filler()
                if reply and (reply.outcome or self.recognition_available):
                    await self._play(reply)
                elif self.recognition_exhausted:
                    await self._recognition_handoff()
                else:
                    self.deferred_reply = reply
                continue
            if event.kind == "speech_lost":
                await self._clear_filler()
                self.recognition_available = False
                self.overlap = False
                self.onset_sample = None
                if not self.turn and (self.reply is None or self.reply.outcome is None):
                    await self._stop()
                if self.call.trace:
                    self.call.trace.add("speech_disconnected")
                continue
            if event.kind in {"speech_restored", "speech_unavailable"}:
                self.recognition_available = event.kind == "speech_restored"
                self.recognition_exhausted = not self.recognition_available
                if self.call.trace:
                    self.call.trace.add(event.kind)
                if self.turn or (self.reply and self.reply.outcome):
                    continue
                if self.reply:
                    await self._stop()
                if self.recognition_available:
                    reply, self.deferred_reply = self.deferred_reply, None
                    await self._play(
                        reply
                        or self.call.say("The connection was interrupted. Please repeat that.")
                    )
                else:
                    self.deferred_reply = None
                    await self._recognition_handoff()
                continue
            if event.kind == "start":
                self.overlap = self.reply is not None or self.turn is not None
                self.onset_sample = (
                    round(SAMPLE_RATE * event.onset) if event.onset is not None else None
                )
                continue
            if event.kind == "final" and EMERGENCY.search(event.text):
                if self.reply and self.reply.outcome == "emergency":
                    continue
                self.last_final = monotonic()
                await self._cancel_turn()
                reply = await self.call.handle(event.text)
                await self._clear_filler()
                if self.reply:
                    await self._stop()
                if self.on_caller:
                    await self.on_caller(event.text)
                await self._play(reply)
                continue
            if self.turn:
                if event.kind in {"interim", "final", "digit"} and (
                    event.kind == "digit" or interrupts(event.text)
                ):
                    await self._clear_filler()
                staff_requested = (event.kind == "final" and STAFF.search(event.text)) or (
                    event.kind == "digit" and event.text == "0" and self.call.stage != "keypad"
                )
                if event.kind in {"final", "digit"}:
                    self.overlap = False
                    self.onset_sample = None
                    if event.kind == "final" and not staff_requested and self.call.trace:
                        self.call.trace.add("caller_turn_skipped", state="processing")
                if staff_requested and self.call.stage != "closed":
                    await self._cancel_turn()
                    await self._play(await self.call.handle(event.text))
                continue
            if event.kind == "mark" and self.reply and event.text == self.reply.id:
                self.call.playback_finished(event.text)
                self.playback_end_sample = self.audio.end
                outcome = self.reply.outcome
                self.reply = None
                if outcome:
                    return outcome
            elif event.kind in {"interim", "final", "digit"}:
                silence = 0
                if self.reply:
                    if self.reply.outcome:
                        continue
                    heard = re.sub(r"[^\w ]", "", event.text.lower()).strip()
                    speaking = re.sub(r"[^\w ]", "", self.reply.text.lower())
                    conversational_answer = (
                        event.kind == "final"
                        and not self.reply.confirmation
                        and self.call.stage in {"other_provider", "day_alternative"}
                        and answer_to_confirmation(event.text.lower()) is not None
                    )
                    if event.kind != "digit" and (
                        (not conversational_answer and not interrupts(event.text))
                        or (heard and f" {heard} " in f" {speaking} ")
                    ):
                        if event.kind == "final" and self.call.trace:
                            self.call.trace.add("caller_turn_skipped", state="playback")
                        continue
                    await self._stop()
                if event.kind == "interim":
                    continue
                # A yes that began during read-back stays ineligible even if the
                # completion mark reached the queue before the final transcript.
                word_sample = round(SAMPLE_RATE * event.onset) if event.onset is not None else None
                began_before_playback_end = self.playback_end_sample is not None and any(
                    sample is not None and sample < self.playback_end_sample
                    for sample in (self.onset_sample, word_sample)
                )
                if self.overlap or began_before_playback_end:
                    self.call.interrupt()
                self.overlap = False
                self.onset_sample = None
                self.last_final = monotonic()
                if event.text.strip().lower().rstrip(".!") in {"stop", "wait"}:
                    await self._play(self.call.say("Okay. Please continue."))
                    continue
                self.turn = asyncio.create_task(self._process_turn(event))
                if self.filler_audio and self.recognition_available and event.kind == "final":
                    self.filler_due = asyncio.get_running_loop().time() + FILLER_DELAY_SECONDS
