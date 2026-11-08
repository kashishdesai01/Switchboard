import asyncio
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, time, timedelta

import httpx

from switchboard.config import BAYVIEW, HORIZON_DAYS
from switchboard.fixtures import synthetic_resources, synthetic_slots
from switchboard.scheduling import instant
from switchboard.seed import fhir_metadata
from switchboard.store import DemoStore


def test_seed_waits_for_transient_metadata_errors(monkeypatch):
    async def run():
        responses = iter([503, 503, 200])
        pauses = []

        async def sleep(delay):
            pauses.append(delay)

        def handle(request):
            assert request.url.path == "/fhir/metadata"
            return httpx.Response(next(responses))

        monkeypatch.setattr("switchboard.seed.asyncio.sleep", sleep)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://fhir/fhir/"
        ) as client:
            assert (await fhir_metadata(client, 10)).status_code == 200
        assert pauses == [2, 2]

    asyncio.run(run())


def test_seed_does_not_retry_a_nontransient_metadata_error():
    async def run():
        def handle(_request):
            return httpx.Response(401)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://fhir/fhir/"
        ) as client:
            assert (await fhir_metadata(client, 1)).status_code == 401

    asyncio.run(run())


def test_fixture_marker_follows_all_seed_resources():
    from switchboard.scheduling import utc_now

    resources = synthetic_resources(BAYVIEW, utc_now())
    assert resources[-1]["resourceType"] == "Organization"
    assert resources[-1]["id"] == f"switchboard-{BAYVIEW.id}"


def test_daily_calendar_is_stable_and_does_not_double_book_doctors(tmp_path):
    now = datetime(2026, 10, 30, 8, tzinfo=BAYVIEW.timezone)
    upcoming = synthetic_slots(BAYVIEW, now)
    tomorrow = synthetic_slots(BAYVIEW, now + timedelta(days=1))
    calendar = defaultdict(list)
    for slot in upcoming:
        start = instant(slot["start"]).astimezone(BAYVIEW.timezone)
        end = instant(slot["end"]).astimezone(BAYVIEW.timezone)
        assert start.date() == end.date()
        assert time(9) <= start.time() < end.time() <= time(17)
        calendar[slot["schedule"]["reference"], start.date()].append((start, end))
    assert len(BAYVIEW.providers) == 6
    assert len(calendar) == 6 * (HORIZON_DAYS + 1)
    for intervals in calendar.values():
        ordered = sorted(intervals)
        assert all(end <= start for (_, end), (start, _) in zip(ordered, ordered[1:], strict=False))
        assert {end - start for start, end in intervals} == {
            timedelta(minutes=20),
            timedelta(minutes=40),
        }
    saturday = now.date() + timedelta(days=1)
    sunday = saturday + timedelta(days=1)
    start_times = [
        {start.time() for start, _ in calendar["Schedule/schedule-brooks", day]}
        for day in (saturday, sunday)
    ]
    assert start_times[0] != start_times[1]

    by_id = {slot["id"]: slot for slot in upcoming}
    assert all(by_id[slot["id"]] == slot for slot in tomorrow if slot["id"] in by_id)
    assert any(slot["id"] not in by_id for slot in tomorrow)
    old_clinic = replace(BAYVIEW, daily_demo_slots=False, providers={"chen": "Dr. Daniel Chen"})
    old = synthetic_slots(old_clinic, now)
    assert all(by_id[slot["id"]] == slot for slot in old)
    booked = {**old[0], "status": "busy"}
    store = DemoStore(tmp_path / "rolling.sqlite")
    store.seed([booked])
    store.seed(upcoming)
    store.seed(tomorrow)
    assert asyncio.run(store.read("Slot", booked["id"]))["status"] == "busy"
