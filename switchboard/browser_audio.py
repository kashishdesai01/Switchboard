import asyncio
import hmac
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection
from websockets.exceptions import WebSocketException

from switchboard.conversation import Call, Reply
from switchboard.voice import Deepgram, Speech, VoiceCall

router = APIRouter()


def local_voice_connection(connection: HTTPConnection) -> bool:
    if connection.client is None:
        return False
    try:
        host = urlsplit("//" + connection.headers.get("host", "")).hostname
        return ip_address(connection.client.host).is_loopback and (
            host == "localhost" or (host is not None and ip_address(host).is_loopback)
        )
    except ValueError:
        return False


class BrowserMedia:
    def __init__(self, socket: WebSocket):
        self.socket = socket

    async def receive(
        self, audio: Callable[[bytes], Awaitable[None]], queue: asyncio.Queue[Speech]
    ) -> None:
        import json

        while True:
            packet = await self.socket.receive()
            if packet["type"] == "websocket.disconnect":
                return
            if payload := packet.get("bytes"):
                if len(payload) != 160:
                    raise ValueError("Expected 20 ms of μ-law audio")
                await audio(payload)
            elif text := packet.get("text"):
                message = json.loads(text)
                match message.get("type"):
                    case "mark":
                        await queue.put(Speech("mark", message["id"]))
                    case "stop":
                        return
                    case _:
                        raise ValueError("Invalid browser media event")

    async def send_audio(self, audio: bytes) -> None:
        await self.socket.send_bytes(audio)

    async def mark(self, name: str) -> None:
        await self.socket.send_json({"type": "mark", "id": name})

    async def clear(self) -> None:
        await self.socket.send_json({"type": "clear"})

    async def caption(self, reply: Reply) -> None:
        await self.socket.send_json({"type": "reply", **asdict(reply)})

    async def caller(self, text: str) -> None:
        await self.socket.send_json({"type": "caller", "text": text})


@router.websocket("/voice-lab")
async def browser_voice(socket: WebSocket):
    state = socket.app.state
    if not state.browser_audio or not state.origin_check(socket) or state.sessions >= 20:
        await socket.close(code=1008)
        return
    key, secret = state.browser_audio
    requires_token = not local_voice_connection(socket)
    if requires_token and not secret:
        await socket.close(code=1008)
        return
    state.sessions += 1
    trace = None
    try:
        await socket.accept()
        if requires_token:
            async with asyncio.timeout(4):
                token = await socket.receive_text()
            if not hmac.compare_digest(token.encode(), secret.encode()):
                await socket.close(code=1008)
                return
        await socket.send_json({"type": "ready", "mode": f"{state.mode} · Streaming audio"})
        adapter = BrowserMedia(socket)
        trace = state.ops.start("browser_audio")
        call = Call(
            state.scheduling,
            state.intent_router,
            trace,
            allow_registration=state.allow_registration,
            frontdesk=state.frontdesk,
            email_form=True,
        )
        voice = VoiceCall(
            call,
            adapter,
            Deepgram(key, state.vendor_http),
            filler_audio=state.filler_audio,
            on_reply=adapter.caption,
            on_caller=adapter.caller,
        )
        async with asyncio.timeout(8 * 60):
            outcome = await voice.run()
        await socket.send_json({"type": "ended", "outcome": outcome})
    except (TimeoutError, ValueError, OSError, WebSocketException, WebSocketDisconnect):
        pass
    finally:
        state.sessions -= 1
        if trace:
            await state.ops.finish(trace)
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close()
