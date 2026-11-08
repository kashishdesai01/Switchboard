"""Verify a running synthetic deployment through its public app and FHIR interfaces."""

import asyncio
import json
import os
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect

from switchboard.scheduling import RECEIPT_SYSTEM


async def main() -> None:
    origin = os.environ.get("SWITCHBOARD_DEMO_URL", "http://127.0.0.1:8765").rstrip("/")
    fhir_url = os.environ["SWITCHBOARD_FHIR_URL"].rstrip("/")
    token = os.environ["SWITCHBOARD_OPERATIONS_TOKEN"]
    parts = urlsplit(origin)
    ws_url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, "/call", "", ""))
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=5) as client:
        (await client.get(f"{origin}/readyz")).raise_for_status()

        async def writes():
            response = await client.get(f"{origin}/api/operations", headers=auth)
            response.raise_for_status()
            return response.json()["settled_mutations"]

        existing = {item["operation"] for item in await writes()}
        async with asyncio.timeout(25), connect(ws_url, origin=origin) as ws:
            assert json.loads(await ws.recv())["mode"] == "FHIR R4"
            assert json.loads(await ws.recv())["type"] == "reply"

            async def say(text):
                await ws.send(json.dumps({"type": "text", "text": text}))
                return json.loads(await ws.recv())

            await say("book a follow-up next week in the afternoon")
            await say("Maria Lopez")
            dob = await say("1984-07-02")
            assert dob["confirmation"]
            await ws.send(json.dumps({"type": "played", "id": dob["id"]}))
            assert "Option 1" in (await say("yes"))["text"]
            proposed = await say("option one")
            assert proposed["confirmation"]
            premature = await say("yes")
            assert premature["confirmation"] and premature["id"] != proposed["id"]
            await ws.send(json.dumps({"type": "played", "id": premature["id"]}))
            assert (await say("yes"))["outcome"] == "booked"

        (operation,) = [
            item["operation"]
            for item in await writes()
            if item["status"] == "committed" and item["operation"] not in existing
        ]
        result = await client.get(
            f"{fhir_url}/Appointment",
            params={"identifier": f"{RECEIPT_SYSTEM}|{operation}"},
            headers={"Accept": "application/fhir+json", "Cache-Control": "no-cache"},
        )
        result.raise_for_status()
        (appointment,) = [item["resource"] for item in result.json().get("entry", [])]
        assert appointment["status"] == "booked"
    print("FHIR-backed booking verified through the WebSocket, operation journal, and Appointment.")


if __name__ == "__main__":
    asyncio.run(main())
