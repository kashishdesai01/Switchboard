"""Run against the isolated, seeded HAPI instance from compose.yaml."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from switchboard.config import BAYVIEW
from switchboard.scheduling import RECEIPT_SYSTEM, Change, Scheduling, utc_now
from switchboard.store import Conflict, FhirStore, Unavailable

URL = os.getenv("SWITCHBOARD_TEST_FHIR_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Set SWITCHBOARD_TEST_FHIR_URL for live HAPI tests")


class LostResponse(FhirStore):
    async def transaction(self, *, updates, creates):
        await super().transaction(updates=updates, creates=creates)
        if creates:
            raise Unavailable("Injected lost transaction response")


async def available_pair(scheduler: Scheduling, visit: str, days_ahead: int):
    first_day = scheduler.now().astimezone(BAYVIEW.timezone).date() + timedelta(days=days_ahead)
    for offset in range(14):
        day = first_day + timedelta(days=offset)
        if day.weekday() < 5:
            options = await scheduler.options(visit, day)
            if len(options) >= 2:
                return options[:2]
    pytest.fail("The seeded HAPI instance needs two free slots on one upcoming weekday")


def test_real_hapi_versioned_writes_and_operation_receipts():
    async def run():
        async with httpx.AsyncClient(
            base_url=URL,
            headers={"Accept": "application/fhir+json", "Content-Type": "application/fhir+json"},
            timeout=10,
        ) as client:
            store = LostResponse(client)
            scheduler = Scheduling(store, BAYVIEW)
            now = utc_now()
            first, second = await available_pair(scheduler, "follow-up", 5)
            hold = await scheduler.hold(first)
            with pytest.raises(Conflict):
                await scheduler.hold(first)
            booking = Change(hold.operation, "book", "maria-demo", None, hold)
            await scheduler.commit(booking)
            await scheduler.commit(booking)
            created = await store.search(
                "Appointment", identifier=f"{RECEIPT_SYSTEM}|{booking.operation}"
            )
            assert len(created) == 1
            assert (await store.read("Slot", first["id"]))["status"] == "busy"

            replacement = await scheduler.hold(second)
            moved = Change(
                replacement.operation, "reschedule", "maria-demo", created[0], replacement
            )
            await scheduler.commit(moved)
            assert (await store.read("Appointment", created[0]["id"]))["status"] == "cancelled"
            assert (await store.read("Slot", first["id"]))["status"] == "free"

            (rescheduled,) = await store.search(
                "Appointment", identifier=f"{RECEIPT_SYSTEM}|{moved.operation}"
            )
            cancellation = Change(uuid4().hex, "cancel", "maria-demo", rescheduled, None)
            await scheduler.commit(cancellation)
            assert (await store.read("Slot", second["id"]))["status"] == "free"

            first_free = await store.read("Slot", first["id"])
            assert first_free and first_free["status"] == "free"
            await scheduler.hold(first_free)
            restarted = Scheduling(store, BAYVIEW, lambda: now + timedelta(minutes=5))
            assert await restarted.expire_holds() >= 1
            assert (await store.read("Slot", first["id"]))["status"] == "free"

    asyncio.run(run())


def test_real_hapi_rolls_back_a_bundle_with_a_stale_slot():
    async def run():
        async with httpx.AsyncClient(
            base_url=URL,
            headers={"Accept": "application/fhir+json", "Content-Type": "application/fhir+json"},
            timeout=10,
        ) as client:
            store = FhirStore(client)
            scheduler = Scheduling(store, BAYVIEW)
            first, second = await available_pair(scheduler, "first visit", 9)
            competing = await asyncio.gather(
                scheduler.hold(second), scheduler.hold(second), return_exceptions=True
            )
            winner = next(item for item in competing if not isinstance(item, Exception))
            assert sum(isinstance(item, Conflict) for item in competing) == 1
            try:
                with pytest.raises(Conflict):
                    await store.transaction(
                        updates=[
                            {**first, "status": "busy"},
                            {**second, "status": "busy"},
                        ],
                        creates=[],
                    )
                first_after = await store.read("Slot", first["id"])
                assert first_after["status"] == "free"
                assert first_after["meta"]["versionId"] == first["meta"]["versionId"]
            finally:
                await scheduler.release(winner)

    asyncio.run(run())


def test_real_hapi_coverage_for_verified_synthetic_patient():
    async def run():
        async with httpx.AsyncClient(
            base_url=URL,
            headers={"Accept": "application/fhir+json"},
            timeout=10,
        ) as client:
            scheduler = Scheduling(FhirStore(client), BAYVIEW)
            assert await scheduler.coverage("maria-demo") == ("Aetna",)
            assert await scheduler.coverage("maria-namesake") == ()

    asyncio.run(run())
