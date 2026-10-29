"""Vapi transcribes and speaks. Switchboard owns the conversation and all writes."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from time import monotonic, time
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import State

from switchboard.conversation import Call, Reply

router = APIRouter(prefix="/vapi")
LOG = logging.getLogger(__name__)
MODEL = "switchboard"
SECRET_HEADER = "X-Switchboard-Vapi-Secret"
MAX_SECONDS = 8 * 60
MAX_TURNS = 160
MAX_BODY_BYTES = 128 * 1024
E164 = re.compile(r"\+[1-9][0-9]{6,14}")
KEYPAD = re.compile(r"User's Keypad Entry:\s*([0-9]{1,8})", re.IGNORECASE)
ENDED = "This demo call has ended. Thank you for calling."
UNAVAILABLE = "I'm sorry, the demo is unavailable for this call. Please hang up and try again."


def same_words(left: str, right: str) -> bool:
    return " ".join(left.split()) == " ".join(right.split())


@dataclass
class Playback:
    reply: Reply
    turn: int | None = None
    stopped_turn: int | None = None
    interrupted: bool = False
    issued_at_ms: int = field(default_factory=lambda: int(time() * 1000))


@dataclass
class Session:
    call: Call | None
    expires: float = field(default_factory=lambda: monotonic() + MAX_SECONDS)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: int = 0
    request_digest: bytes = b""
    reply: str = ""
    playback: Playback | None = None


class VapiCalls:
    def __init__(self, state: State, secret: str, callers: frozenset[str]):
        self.state = state
        self.secret = secret.encode()
        self.callers = callers
        self.sessions: dict[str, Session] = {}
        self.ended: OrderedDict[str, float] = OrderedDict()

    @classmethod
    def configured(cls, state: State) -> "VapiCalls | None":
        secret = os.getenv("SWITCHBOARD_VAPI_SECRET")
        if not secret:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
            raise RuntimeError("Use a random Vapi secret of at least 32 URL-safe characters")
        callers = frozenset(
            part.strip()
            for part in os.getenv("SWITCHBOARD_VAPI_CALLERS", "").split(",")
            if part.strip()
        )
        if not callers or any(not E164.fullmatch(caller) for caller in callers):
            raise RuntimeError("Set SWITCHBOARD_VAPI_CALLERS to authorized E.164 test numbers")
        return cls(state, secret, callers)

    def open(self, call_id: str) -> Session | None:
        if call_id in self.ended:
            return None
        if self.state.sessions >= 20 or len(self.sessions) >= 80:
            return None
        call = Call(
            self.state.scheduling,
            self.state.intent_router,
            self.state.ops.start("vapi"),
            allow_registration=self.state.allow_registration,
            frontdesk=self.state.frontdesk,
        )
        session = Session(call)
        self.sessions[call_id] = session
        self.state.sessions += 1
        return session

    async def finish(self, session: Session) -> None:
        call, session.call = session.call, None
        session.playback = None
        if call is None:
            return
        try:
            await call.close()
        finally:
            self.state.sessions -= 1
            assert call.trace is not None
            await self.state.ops.finish(call.trace)

    async def end(self, call_id: str) -> None:
        self.ended[call_id] = monotonic() + MAX_SECONDS
        self.ended.move_to_end(call_id)
        if len(self.ended) > 1024:
            self.ended.popitem(last=False)
        session = self.sessions.pop(call_id, None)
        if session:
            async with session.lock:
                await self.finish(session)

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = monotonic()
            for call_id, session in list(self.sessions.items()):
                if session.expires <= now:
                    try:
                        await self.end(call_id)
                    except Exception:
                        LOG.exception("Vapi session cleanup failed")
            while self.ended and next(iter(self.ended.values())) <= now:
                self.ended.popitem(last=False)

    async def shutdown(self) -> None:
        for call_id in list(self.sessions):
            try:
                await self.end(call_id)
            except Exception:
                LOG.exception("Vapi session cleanup failed")


async def authorize(request: Request) -> VapiCalls:
    calls: VapiCalls | None = request.app.state.vapi
    if calls is None:
        raise HTTPException(404)
    supplied = request.headers.get(SECRET_HEADER, "").encode()
    if not hmac.compare_digest(supplied, calls.secret):
        raise HTTPException(401, "Invalid Vapi credential")
    return calls


async def read_object(request: Request) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "Vapi request is too large")
        data.extend(chunk)
    try:
        result = json.loads(data)
    except (ValueError, RecursionError):
        raise HTTPException(400, "Invalid Vapi JSON") from None
    if not isinstance(result, dict):
        raise HTTPException(400, "Expected a Vapi object")
    return result


def object_field(value: dict, field: str) -> dict:
    item = value.get(field)
    return item if isinstance(item, dict) else {}


def id_of(call: dict) -> str:
    try:
        return str(UUID(str(call.get("id"))))
    except ValueError:
        raise HTTPException(400, "Missing or invalid Vapi call ID. Enable call metadata.") from None


def speech(text: str, stream: bool) -> Response:
    common = {"id": f"chatcmpl-{uuid4().hex}", "created": int(time()), "model": MODEL}
    if not stream:
        return JSONResponse(
            {
                **common,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    def events() -> Iterator[str]:
        for delta, reason in [
            ({"role": "assistant", "content": text}, None),
            ({}, "stop"),
        ]:
            frame = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
            yield f"data: {json.dumps(frame)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


def caller_turn(data: dict) -> tuple[int, str, str, bytes]:
    messages = data.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_TURNS * 2 + 8:
        raise HTTPException(400, "Invalid Vapi conversation history")
    history = []
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(400, "Invalid Vapi message")
        role, content = message.get("role"), message.get("content")
        if role in ("assistant", "user") and isinstance(content, str):
            history.append((role, content.strip()))
    if not history or history[-1][0] != "user":
        raise HTTPException(400, "Expected a caller turn")
    count = sum(role == "user" for role, _ in history)
    text = history[-1][1]
    previous = history[-2][1] if len(history) >= 2 and history[-2][0] == "assistant" else ""
    digest = hashlib.sha256(json.dumps(history).encode()).digest()
    return count, text, previous, digest


async def handle_turn(call: Call, text: str) -> Reply:
    keypad = KEYPAD.fullmatch(text)
    if keypad and call.stage == "keypad":
        for digit in keypad[1]:
            reply = await call.digit(digit)
            if reply is not None:
                return reply
        return call.say("Please enter the remaining digits of your date of birth.")
    if keypad and keypad[1] in {"0", "1", "2", "3"}:
        reply = await call.digit(keypad[1])
        if reply is not None:
            return reply
    return await call.handle(text)


@router.post("/chat/completions")
async def completion(request: Request, calls: Annotated[VapiCalls, Depends(authorize)]) -> Response:
    data = await read_object(request)
    metadata = object_field(data, "call")
    call_id = id_of(metadata)
    customer = object_field(metadata, "customer") or object_field(data, "customer")
    number = customer.get("number")
    stream = data.get("stream") is True
    if (
        metadata.get("type") != "inboundPhoneCall"
        or not isinstance(number, str)
        or number not in calls.callers
    ):
        return speech("This demo accepts calls from configured test numbers only.", stream)
    count, text, previous, digest = caller_turn(data)
    session = calls.sessions.get(call_id)
    if session is None:
        if count != 1 or call_id in calls.ended:
            return speech(ENDED, stream)
        session = calls.open(call_id)
        if session is None:
            return speech(UNAVAILABLE, stream)
    async with session.lock:
        if call_id in calls.ended or session.expires <= monotonic():
            return speech(ENDED, stream)
        if count == session.turns and digest == session.request_digest:
            return speech(session.reply, stream)
        call = session.call
        if call is None:
            return speech(ENDED, stream)
        if count != session.turns + 1:
            return speech("I couldn't follow that reply. Please repeat it.", stream)
        playback = session.playback
        session.playback = None
        if not 1 <= len(text) <= 400:
            call.interrupt()
            session.turns, session.request_digest = count, digest
            session.reply = "Could you say that again in a shorter sentence?"
            return speech(session.reply, stream)
        if (
            playback
            and playback.turn is not None
            and playback.stopped_turn == playback.turn
            and not playback.interrupted
            and same_words(previous, playback.reply.text)
        ):
            call.playback_finished(playback.reply.id)
        elif playback and call.trace:
            call.trace.add("playback_unconfirmed")
        try:
            reply = await handle_turn(call, text)
        except Exception:
            LOG.error("Vapi caller turn failed")
            await calls.finish(session)
            return speech(UNAVAILABLE, stream)
        session.turns, session.request_digest, session.reply = count, digest, reply.text
        if reply.outcome:
            await calls.finish(session)
        elif reply.confirmation:
            session.playback = Playback(reply)
        return speech(reply.text, stream)


@router.post("/events", status_code=204)
async def event(request: Request, calls: Annotated[VapiCalls, Depends(authorize)]) -> Response:
    message = object_field(await read_object(request), "message")
    kind = message.get("type")
    ended = kind == "end-of-call-report" or (
        kind == "status-update" and message.get("status") == "ended"
    )
    if not ended and kind not in ("assistant.speechStarted", "speech-update", "user-interrupted"):
        return Response(status_code=204)
    call_id = id_of(object_field(message, "call"))
    if ended:
        await calls.end(call_id)
    elif session := calls.sessions.get(call_id):
        async with session.lock:
            if playback := session.playback:
                turn = message.get("turn")
                timestamp = message.get("timestamp")
                if type(timestamp) in (int, float) and timestamp < playback.issued_at_ms:
                    return Response(status_code=204)
                if kind == "user-interrupted":
                    playback.interrupted = True
                elif kind == "assistant.speechStarted" and type(turn) is int:
                    spoken = message.get("text")
                    if message.get("source") == "model" and isinstance(spoken, str):
                        if same_words(spoken, playback.reply.text):
                            playback.turn = turn
                elif (
                    kind == "speech-update"
                    and message.get("status") == "stopped"
                    and message.get("role") == "assistant"
                    and type(turn) is int
                ):
                    playback.stopped_turn = turn
    return Response(status_code=204)
