"""Twilio control plane. Enabled only for explicitly allowlisted demo callers."""

import asyncio
import base64
import hashlib
import hmac
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from time import time
from urllib.parse import parse_qsl, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator
from websockets.exceptions import WebSocketException

from switchboard.conversation import EMERGENCY_SCRIPT, Call
from switchboard.voice import Deepgram, Speech, VoiceCall

router = APIRouter(prefix="/twilio")
SID = re.compile(r"CA[0-9a-fA-F]{32}")
NUMBER = re.compile(r"\+[1-9]\d{7,14}")
FORMAT = {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}


@dataclass(frozen=True)
class TwilioConfig:
    origin: str
    auth_token: str
    account: str
    deepgram_key: str
    callers: frozenset[str]
    staff: str | None

    @classmethod
    def from_env(cls) -> "TwilioConfig | None":
        origin = os.getenv("SWITCHBOARD_PUBLIC_ORIGIN", "").rstrip("/")
        if not origin:
            return None
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.port is not None
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise RuntimeError("SWITCHBOARD_PUBLIC_ORIGIN must be an HTTPS origin")
        callers = frozenset(os.environ["SWITCHBOARD_DEMO_CALLERS"].split(","))
        staff = os.getenv("SWITCHBOARD_DEMO_STAFF")
        if not all(NUMBER.fullmatch(number) for number in callers | ({staff} if staff else set())):
            raise RuntimeError("Configure caller and staff numbers in E.164 format")
        return cls(
            origin,
            os.environ["TWILIO_AUTH_TOKEN"],
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["DEEPGRAM_API_KEY"],
            callers,
            staff,
        )

    @property
    def media_url(self) -> str:
        return "wss:" + self.origin.removeprefix("https:") + "/twilio/media"


class StreamTickets:
    def __init__(self, path: Path, secret: str):
        self.path = path
        self.secret = secret.encode()
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS streams(
                    sid TEXT PRIMARY KEY, digest TEXT NOT NULL, expires INTEGER NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0, outcome TEXT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS demo_dials(
                    sid TEXT PRIMARY KEY, status TEXT NOT NULL, updated INTEGER NOT NULL
                )
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=1)

    def mint(self, sid: str) -> str | None:
        token = hmac.digest(self.secret, b"switchboard-media:" + sid.encode(), "sha256").hex()
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = int(time())
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM streams WHERE expires < ?", (now - 86400,))
            db.execute("DELETE FROM demo_dials WHERE updated < ?", (now - 86400,))
            db.execute(
                "INSERT OR IGNORE INTO streams(sid,digest,expires) VALUES (?,?,?)",
                (sid, digest, now + 60),
            )
            entry = db.execute(
                "SELECT 1 FROM streams WHERE sid=? AND consumed=0 AND expires>=?", (sid, now)
            ).fetchone()
        return token if entry else None

    def consume(self, sid: str, token: str) -> bool:
        with closing(self.connect()) as db, db:
            return (
                db.execute(
                    "UPDATE streams SET consumed=1 WHERE sid=? AND digest=? AND consumed=0 AND expires>=?",
                    (sid, hashlib.sha256(token.encode()).hexdigest(), int(time())),
                ).rowcount
                == 1
            )

    def finish(self, sid: str, outcome: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("UPDATE streams SET outcome=? WHERE sid=?", (outcome, sid))

    def dial(self, sid: str, status: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO demo_dials VALUES (?, ?, ?)
                   ON CONFLICT(sid) DO UPDATE SET status=excluded.status, updated=excluded.updated
                   WHERE demo_dials.status='attempted'""",
                (sid, status, int(time())),
            )

    def outcome(self, sid: str) -> str | None:
        with closing(self.connect()) as db:
            entry = db.execute("SELECT outcome FROM streams WHERE sid=?", (sid,)).fetchone()
        return entry[0] if entry else None


def xml(root: Element) -> Response:
    return Response(tostring(root, encoding="unicode"), media_type="application/xml")


def end(root: Element, text: str = "") -> Response:
    if text:
        SubElement(root, "Say").text = text
    SubElement(root, "Hangup")
    return xml(root)


async def form(request: Request) -> tuple[TwilioConfig, StreamTickets, FormData]:
    config = request.app.state.twilio
    if not config:
        raise HTTPException(404)
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16_384:
            raise HTTPException(413)
    try:
        data = FormData(parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=128))
    except ValueError as exc:
        raise HTTPException(400) from exc
    url = config.origin + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if not RequestValidator(config.auth_token).validate(
        url, data, request.headers.get("X-Twilio-Signature", "")
    ):
        raise HTTPException(403)
    if data.get("AccountSid") != config.account or not SID.fullmatch(str(data.get("CallSid", ""))):
        raise HTTPException(403)
    return config, request.app.state.tickets, data


@router.post("/voice")
async def inbound(request: Request):
    config, tickets, data = await form(request)
    root = Element("Response")
    if data.get("From") not in config.callers:
        SubElement(root, "Reject")
        return xml(root)
    token = tickets.mint(str(data["CallSid"]))
    if not token:
        return end(root, "The demo is unavailable. Please try a new call.")
    connect = SubElement(root, "Connect")
    stream = SubElement(connect, "Stream", {"url": config.media_url})
    SubElement(stream, "Parameter", {"name": "ticket", "value": token})
    SubElement(root, "Redirect", {"method": "POST"}).text = config.origin + "/twilio/after-stream"
    return xml(root)


@router.post("/after-stream")
async def after_stream(request: Request):
    config, tickets, data = await form(request)
    root = Element("Response")
    outcome = tickets.outcome(str(data["CallSid"]))
    if outcome in {"handoff", "needs_review"} and config.staff:
        tickets.dial(str(data["CallSid"]), "attempted")
        dial = SubElement(
            root,
            "Dial",
            {
                "timeout": "15",
                "action": config.origin + "/twilio/dial-result",
                "method": "POST",
            },
        )
        SubElement(dial, "Number").text = config.staff
        return xml(root)
    if outcome == "emergency":
        return end(root)
    if outcome == "emergency_unplayed":
        return end(root, EMERGENCY_SCRIPT)
    if outcome in {None, "failure", "abandoned"}:
        return end(root, "The automated demo is unavailable. Please try again later.")
    return end(root)


@router.post("/dial-result")
async def dial_result(request: Request):
    _, tickets, data = await form(request)
    status = str(data.get("DialCallStatus", "failed"))
    known = {"completed", "answered", "busy", "no-answer", "failed", "canceled"}
    tickets.dial(str(data["CallSid"]), status if status in known else "failed")
    return end(
        Element("Response"),
        ""
        if data.get("DialCallStatus") == "completed"
        else "Staff are unavailable. Please try again later.",
    )


class TwilioMedia:
    def __init__(self, socket: WebSocket, sid: str):
        self.socket = socket
        self.sid = sid

    async def receive(
        self, audio: Callable[[bytes], Awaitable[None]], queue: asyncio.Queue[Speech]
    ) -> None:
        while True:
            try:
                packet = await self.socket.receive_json()
            except WebSocketDisconnect:
                return
            if packet.get("streamSid") != self.sid:
                raise ValueError("Incorrect stream SID")
            match packet.get("event"):
                case "media":
                    payload = base64.b64decode(packet["media"]["payload"], validate=True)
                    if not 0 < len(payload) <= 3200:
                        raise ValueError("Invalid media payload size")
                    await audio(payload)
                case "mark":
                    await queue.put(Speech("mark", packet["mark"]["name"]))
                case "dtmf":
                    await queue.put(Speech("digit", packet["dtmf"]["digit"]))
                case "stop":
                    return

    async def send_audio(self, audio: bytes) -> None:
        await self.socket.send_json(
            {
                "event": "media",
                "streamSid": self.sid,
                "media": {"payload": base64.b64encode(audio).decode()},
            }
        )

    async def mark(self, name: str) -> None:
        await self.socket.send_json(
            {"event": "mark", "streamSid": self.sid, "mark": {"name": name}}
        )

    async def clear(self) -> None:
        await self.socket.send_json({"event": "clear", "streamSid": self.sid})


@router.websocket("/media")
async def media(socket: WebSocket):
    config = socket.app.state.twilio
    signature = socket.headers.get("X-Twilio-Signature", "")
    if (
        not config
        or socket.url.query
        or not any(
            RequestValidator(config.auth_token).validate(url, {}, signature)
            for url in (config.media_url, config.media_url + "/")
        )
    ):
        await socket.close(code=1008)
        return
    if socket.app.state.sessions >= 20:
        await socket.close(code=1013)
        return
    socket.app.state.sessions += 1
    tickets = socket.app.state.tickets
    claimed_sid = None
    trace = None
    outcome = "failure"
    try:
        await socket.accept()
        async with asyncio.timeout(5):
            if (await socket.receive_json()).get("event") != "connected":
                return
            packet = await socket.receive_json()
            if packet.get("event") != "start":
                return
            start = packet["start"]
            sid = start["callSid"]
            if (
                start.get("accountSid") != config.account
                or start.get("mediaFormat") != FORMAT
                or not SID.fullmatch(sid)
                or not tickets.consume(sid, start["customParameters"]["ticket"])
            ):
                return
            claimed_sid = sid
        adapter = TwilioMedia(socket, start["streamSid"])
        trace = socket.app.state.ops.start("twilio")
        voice = VoiceCall(
            Call(
                socket.app.state.scheduling,
                socket.app.state.intent_router,
                trace,
                frontdesk=socket.app.state.frontdesk,
                allow_registration=socket.app.state.allow_registration,
            ),
            adapter,
            Deepgram(config.deepgram_key, socket.app.state.vendor_http),
            filler_audio=socket.app.state.filler_audio,
        )
        async with asyncio.timeout(8 * 60):
            outcome = await voice.run()
    except (
        ValueError,
        KeyError,
        TypeError,
        TimeoutError,
        OSError,
        WebSocketException,
        WebSocketDisconnect,
    ):
        pass
    finally:
        socket.app.state.sessions -= 1
        if trace and trace.outcome == "emergency" and outcome != "emergency":
            outcome = "emergency_unplayed"
        if claimed_sid:
            tickets.finish(claimed_sid, outcome)
        if trace:
            trace.add(f"media_{outcome}")
            await socket.app.state.ops.finish(trace)
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close()
