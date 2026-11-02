import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from switchboard.browser_audio import local_voice_connection
from switchboard.browser_audio import router as audio_router
from switchboard.config import HORIZON_DAYS, Clinic, configured_clinic
from switchboard.conversation import Call, Reply
from switchboard.fixtures import synthetic_resources, synthetic_slots
from switchboard.frontdesk import FrontDesk, MailSettings
from switchboard.intents import ClaudeRouter
from switchboard.operations import Operations
from switchboard.operations import router as operations_router
from switchboard.scheduling import Scheduling, utc_now
from switchboard.store import DemoStore, FhirStore, Unavailable
from switchboard.telephony import StreamTickets, TwilioConfig, router
from switchboard.vapi import VapiCalls
from switchboard.vapi import router as vapi_router
from switchboard.voice import Deepgram, render_filler

STATIC = Path(__file__).parent / "static"
LOG = logging.getLogger(__name__)
MAX_SESSIONS = 20
MAX_CALL_SECONDS = 8 * 60


def same_origin(socket: WebSocket) -> bool:
    try:
        origin = urlsplit(socket.headers.get("origin", ""))
    except ValueError:
        return False
    return origin.scheme in {"http", "https"} and origin.netloc == socket.headers.get("host")


async def sweep(scheduling: Scheduling) -> None:
    while True:
        try:
            await scheduling.expire_holds()
        except Unavailable:
            LOG.warning("Hold expiry store unavailable")
        await asyncio.sleep(5)


async def refresh_demo_slots(store: DemoStore, clinic: Clinic, seeded_at: datetime) -> None:
    seeded_day = seeded_at.astimezone(clinic.timezone).date()
    while True:
        await asyncio.sleep(60)
        now = utc_now()
        day = now.astimezone(clinic.timezone).date()
        if day <= seeded_day:
            continue
        try:
            first = max(0, HORIZON_DAYS + 1 - (day - seeded_day).days)
            await asyncio.to_thread(store.seed, synthetic_slots(clinic, now, first_offset=first))
        except sqlite3.Error:
            LOG.warning("Demo availability refresh will retry")
        else:
            seeded_day = day


async def reconcile(scheduling: Scheduling, ops: Operations) -> None:
    while True:
        try:
            await ops.reconcile(scheduling.receipt)
        except (Unavailable, sqlite3.Error):
            LOG.warning("Mutation reconciliation unavailable")
        try:
            await ops.purge()
        except sqlite3.Error:
            LOG.warning("Trace retention worker unavailable")
        await asyncio.sleep(30)


async def warm_filler(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.state.filler_audio = await render_filler(Deepgram(os.environ["DEEPGRAM_API_KEY"], client))


async def frontdesk_worker(scheduling: Scheduling, frontdesk: FrontDesk) -> None:
    while True:
        try:
            await frontdesk.reconcile_notices(scheduling)
            await frontdesk.deliver(scheduling)
            await frontdesk.match_waitlist(scheduling)
            await frontdesk.purge()
        except (Unavailable, sqlite3.Error):
            LOG.warning("Front desk background work will resume on the next pass")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    clinic = configured_clinic()
    fhir_url = os.getenv("SWITCHBOARD_FHIR_URL")
    async with (
        httpx.AsyncClient(
            base_url=fhir_url or "http://unused.invalid",
            timeout=httpx.Timeout(2),
            headers={"Accept": "application/fhir+json", "Content-Type": "application/fhir+json"},
        ) as fhir_client,
        httpx.AsyncClient(timeout=httpx.Timeout(4)) as vendor_client,
    ):
        seeded_at = utc_now()
        if fhir_url:
            store = FhirStore(fhir_client)
            if not await store.read("Organization", f"switchboard-{clinic.id}"):
                raise RuntimeError(
                    "Seed a dedicated synthetic FHIR instance before starting Switchboard"
                )
        else:
            store = DemoStore(Path(os.getenv("SWITCHBOARD_DB", "switchboard.sqlite")))
            store.seed(synthetic_resources(clinic, seeded_at))
        app.state.scheduling = Scheduling(store, clinic)
        app.state.mode = "FHIR R4" if fhir_url else "Local simulator"
        app.state.allow_registration = (
            not fhir_url or os.getenv("SWITCHBOARD_FHIR_REGISTRATION") == "1"
        )
        app.state.sessions = 0
        app.state.vendor_http = vendor_client
        app.state.origin_check = same_origin
        access = os.getenv("SWITCHBOARD_BROWSER_AUDIO_TOKEN")
        if access and len(access) < 24:
            raise RuntimeError("Use a browser audio token of at least 24 characters")
        key = os.getenv("DEEPGRAM_API_KEY")
        app.state.browser_audio = (key, access) if key else None
        model = os.getenv("SWITCHBOARD_CLAUDE_MODEL")
        app.state.intent_router = (
            ClaudeRouter(vendor_client, os.environ["ANTHROPIC_API_KEY"], model) if model else None
        )
        app.state.twilio = TwilioConfig.from_env()
        if app.state.twilio:
            app.state.tickets = StreamTickets(
                Path(os.getenv("SWITCHBOARD_TELEPHONY_DB", "telephony.sqlite")),
                app.state.twilio.auth_token,
            )
        app.state.ops_token = os.getenv("SWITCHBOARD_OPERATIONS_TOKEN")
        if app.state.ops_token and len(app.state.ops_token) < 24:
            raise RuntimeError("Use an operations token of at least 24 characters")
        ops_path = os.getenv("SWITCHBOARD_OPERATIONS_DB")
        if ops_path is None:
            ops_path = str(
                Path(os.getenv("SWITCHBOARD_DB", "switchboard.sqlite")).with_suffix(".ops.sqlite")
            )
        app.state.ops = Operations(
            Path(ops_path),
            f"{clinic.id}@{clinic.version}",
            capture_transcripts=bool(
                app.state.ops_token
                and not fhir_url
                and os.getenv("SWITCHBOARD_STAFF_TRANSCRIPTS") == "1"
            ),
        )
        app.state.frontdesk = FrontDesk(app.state.ops, MailSettings.configured())
        app.state.ops.frontdesk = app.state.frontdesk
        app.state.vapi = vapi = VapiCalls.configured(app.state)
        app.state.browser_router = (
            app.state.intent_router
            if not fhir_url and os.getenv("SWITCHBOARD_ENABLE_BROWSER_MODEL") == "1"
            else None
        )
        app.state.filler_audio = b""
        workers = [
            asyncio.create_task(sweep(app.state.scheduling)),
            asyncio.create_task(reconcile(app.state.scheduling, app.state.ops)),
            asyncio.create_task(frontdesk_worker(app.state.scheduling, app.state.frontdesk)),
        ]
        if isinstance(store, DemoStore) and clinic.daily_demo_slots:
            workers.append(asyncio.create_task(refresh_demo_slots(store, clinic, seeded_at)))
        if app.state.browser_audio or app.state.twilio:
            workers.append(asyncio.create_task(warm_filler(app, vendor_client)))
        if vapi:
            workers.append(asyncio.create_task(vapi.reap()))
        try:
            yield
        finally:
            if vapi:
                await vapi.shutdown()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
app.include_router(router)
app.include_router(audio_router)
app.include_router(vapi_router)
app.include_router(operations_router)


@app.get("/")
async def index():
    return FileResponse(
        STATIC / "index.html",
        headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/capabilities")
async def capabilities(request: Request):
    browser_audio = app.state.browser_audio
    local = local_voice_connection(request)
    return {
        "streaming_audio": bool(browser_audio and (local or browser_audio[1])),
        "streaming_audio_token_required": not local,
        "operations": bool(app.state.ops_token),
        "clinic": app.state.scheduling.clinic.name,
        "mail_delivery": bool(app.state.frontdesk.mail.host),
    }


class EmailRequest(BaseModel):
    token: str = Field(min_length=40, max_length=64)
    address: str = Field(min_length=3, max_length=254)


@app.post("/api/appointment-email")
async def appointment_email(request: EmailRequest):
    try:
        status = await app.state.frontdesk.request_notice(
            request.token,
            request.address,
            app.state.scheduling,
        )
    except (Unavailable, sqlite3.Error) as exc:
        raise HTTPException(503, "Email status is temporarily unavailable") from exc
    return JSONResponse({"status": status}, headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/readyz")
async def readiness():
    scheduling = app.state.scheduling
    try:
        marker = await scheduling.store.read("Organization", f"switchboard-{scheduling.clinic.id}")
    except Unavailable:
        marker = None
    if not marker:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


async def send_reply(socket: WebSocket, reply: Reply, started: float) -> None:
    await socket.send_json(
        {
            "type": "reply",
            **asdict(reply),
            "latency_ms": round((monotonic() - started) * 1000),
        }
    )


@app.websocket("/call")
async def browser_call(socket: WebSocket):
    if not same_origin(socket):
        await socket.close(code=1008)
        return
    if app.state.sessions >= MAX_SESSIONS:
        await socket.close(code=1013)
        return
    app.state.sessions += 1
    trace = app.state.ops.start("browser_text")
    call = Call(
        app.state.scheduling,
        app.state.browser_router,
        trace,
        allow_registration=app.state.allow_registration,
        frontdesk=app.state.frontdesk,
        email_form=True,
    )
    try:
        await socket.accept()
        await socket.send_json({"type": "ready", "mode": app.state.mode})
        reply = call.greeting()
        await send_reply(socket, reply, monotonic())
        async with asyncio.timeout(MAX_CALL_SECONDS):
            for _ in range(160):
                packet = await socket.receive_json()
                if not isinstance(packet, dict):
                    await socket.close(code=1008)
                    return
                match packet.get("type"):
                    case "displayed" | "played":
                        call.playback_finished(packet.get("id"))
                    case "interrupt":
                        # Older browser clients interrupt local audio when a caller replies.
                        # They render the whole read-back before starting that audio.
                        if reply.confirmation:
                            call.playback_finished(reply.id)
                        trace.add("interrupted")
                    case "text":
                        text = packet.get("text")
                        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 400:
                            await socket.close(code=1008)
                            return
                        call.playback_finished(packet.get("reply_to"))
                        started = monotonic()
                        reply = await call.handle(text)
                        await send_reply(socket, reply, started)
                        if reply.outcome:
                            # The browser plays the final response locally after disconnect.
                            await socket.close()
                            return
                    case _:
                        await socket.close(code=1008)
                        return
            await socket.close(code=1008)
    except (TimeoutError, ValueError):
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close(code=1008)
    except WebSocketDisconnect:
        pass
    finally:
        app.state.sessions -= 1
        try:
            await call.close()
        finally:
            await app.state.ops.finish(trace)
