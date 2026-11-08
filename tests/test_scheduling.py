import asyncio
from datetime import UTC, datetime, timedelta

from switchboard.config import BAYVIEW
from switchboard.conversation import Call
from switchboard.fixtures import synthetic_resources
from switchboard.scheduling import Change, Scheduling
from switchboard.store import Conflict, DemoStore, Unavailable

NOW = datetime(2026, 11, 10, 16, tzinfo=UTC)


def setup(tmp_path, store_type=DemoStore):
    store = store_type(tmp_path / "fhir.sqlite")
    store.seed(synthetic_resources(BAYVIEW, NOW))
    return store, Scheduling(store, BAYVIEW, lambda: NOW)


def test_conflicting_hold_and_expiry_after_restart(tmp_path):
    async def run():
        store, scheduler = setup(tmp_path)
        slot, *_ = await scheduler.options("follow-up")
        result = await asyncio.gather(
            scheduler.hold(slot), scheduler.hold(slot), return_exceptions=True
        )
        assert sum(isinstance(item, Conflict) for item in result) == 1
        recovered = Scheduling(DemoStore(store.path), BAYVIEW, lambda: NOW + timedelta(minutes=4))
        assert await recovered.expire_holds() == 1
        assert (await store.read("Slot", slot["id"]))["status"] == "free"

    asyncio.run(run())


def test_lost_write_response_and_idempotent_retry(tmp_path):
    class LostResponse(DemoStore):
        async def transaction(self, *, updates, creates):
            await super().transaction(updates=updates, creates=creates)
            if creates:
                raise Unavailable("Injected loss after commit")

    async def run():
        store, scheduler = setup(tmp_path, LostResponse)
        slot, *_ = await scheduler.options("follow-up")
        hold = await scheduler.hold(slot)
        change = Change(hold.operation, "book", "maria-demo", None, hold)
        await scheduler.commit(change)
        await scheduler.commit(change)
        assert len(await store.search("Appointment")) == 1
        assert len(await store.search("Basic")) == 1
        assert (await store.read("Slot", slot["id"]))["status"] == "busy"

    asyncio.run(run())


async def authenticate(call, intent):
    await call.handle(intent)
    await call.handle("Maria Lopez")
    reply = await call.handle("1984-07-02")
    call.playback_finished(reply.id)
    await call.handle("yes")


def test_booking_rejects_stale_mark_and_ambiguous_yes(tmp_path):
    async def run():
        store, scheduler = setup(tmp_path)
        call = Call(scheduler)
        await authenticate(call, "book a follow-up")
        prompt = await call.handle("option one")
        call.interrupt()
        call.playback_finished(prompt.id)
        repeated = await call.handle("yes")
        assert repeated.confirmation
        assert not await store.search("Appointment")
        call.playback_finished(repeated.id)
        changed = await call.handle("yes but next week")
        assert not await store.search("Appointment")
        assert not await store.search("Slot", status="busy-tentative")
        assert "November 16" in changed.text
        repeated = await call.handle("option one")
        call.playback_finished(repeated.id)
        booked = await call.handle("yes")
        assert booked.outcome == "booked"
        assert len(await store.search("Appointment")) == 1

    asyncio.run(run())


def test_book_move_cancel_uses_bound_readback(tmp_path):
    async def run():
        store, scheduler = setup(tmp_path)
        for action, outcome in [
            ("book next week", "booked"),
            ("reschedule next week", "rescheduled"),
            ("cancel", "cancelled"),
        ]:
            call = Call(scheduler)
            await authenticate(call, action)
            reply = await call.handle("option one")
            if action.startswith("reschedule"):
                reply = await call.handle("option one")
            assert reply.confirmation
            call.playback_finished(reply.id)
            assert (await call.handle("yes")).outcome == outcome
        appointments = await store.search("Appointment")
        assert len(appointments) == 2
        assert {item["status"] for item in appointments} == {"cancelled"}
        assert not await store.search("Slot", status="busy")

    asyncio.run(run())


def test_safety_preempts_confirmation(tmp_path):
    async def run():
        store, scheduler = setup(tmp_path)
        call = Call(scheduler)
        await authenticate(call, "book a follow-up")
        readback = await call.handle("option one")
        call.playback_finished(readback.id)
        assert (await call.handle("yes, and I have chest pain")).outcome == "emergency"
        await call.close()
        assert not await store.search("Appointment")
        assert not await store.search("Slot", status="busy-tentative")

    asyncio.run(run())
