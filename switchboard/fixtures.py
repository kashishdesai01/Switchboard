from datetime import date, datetime, time, timedelta
from random import Random

from switchboard.config import HORIZON_DAYS, VISIT_MINUTES, Clinic
from switchboard.scheduling import SYSTEM
from switchboard.store import Resource

LEGACY_BLOCKS = [(540, 40), (580, 20), (615, 40), (655, 20), (780, 40), (820, 20)]


def _blocks(clinic: Clinic, provider: str, day: date) -> list[tuple[int, int]]:
    if not clinic.daily_demo_slots:
        return LEGACY_BLOCKS if day.weekday() < 5 else []
    # The same clinic, doctor and date always yield the same appointments. Leave the
    # old intervals intact for the two original providers, including in a seeded EHR.
    preserve = provider in clinic.preserve_demo_slots_for and day.weekday() < 5
    windows = (
        [(675, 780), (840, 930), (930, 1020)] if preserve else [(540, 700), (700, 860), (860, 1020)]
    )
    rng = Random(f"{clinic.id}/{provider}/{day.isoformat()}/v1")
    blocks = list(LEGACY_BLOCKS) if preserve else []
    for opening, closing in windows:
        durations = list(VISIT_MINUTES.values())
        rng.shuffle(durations)
        # Allow a break between appointments, with both starts on five-minute boundaries.
        first, second = durations
        start = rng.randrange(opening, closing - first - second - 5 + 1, 5)
        next_start = rng.randrange(start + first + 5, closing - second + 1, 5)
        blocks.extend([(start, first), (next_start, second)])
    return blocks


def synthetic_slots(
    clinic: Clinic, now: datetime, *, first_offset: int | None = None
) -> list[Resource]:
    today = now.astimezone(clinic.timezone).date()
    if first_offset is None:
        first_offset = 0 if clinic.daily_demo_slots else 1
    resources: list[Resource] = []
    for offset in range(first_offset, HORIZON_DAYS + 1):
        day = today + timedelta(days=offset)
        for provider in clinic.providers:
            for minute, duration in _blocks(clinic, provider, day):
                hour, minute = divmod(minute, 60)
                start = datetime.combine(day, time(hour, minute), clinic.timezone)
                resources.append(
                    {
                        "resourceType": "Slot",
                        "id": f"{provider}-{day}-{hour:02d}{minute:02d}",
                        "schedule": {"reference": f"Schedule/schedule-{provider}"},
                        "status": "free",
                        "start": start.isoformat(),
                        "end": (start + timedelta(minutes=duration)).isoformat(),
                    }
                )
    return resources


def synthetic_resources(clinic: Clinic, now: datetime) -> list[Resource]:
    resources: list[Resource] = [
        {
            "resourceType": "Patient",
            "id": "maria-demo",
            "active": True,
            "name": [{"given": ["Maria"], "family": "Lopez"}],
            "birthDate": "1984-07-02",
            "telecom": [{"system": "email", "value": "maria.lopez@example.test"}],
        },
        {
            "resourceType": "Patient",
            "id": "maria-namesake",
            "active": True,
            "name": [{"given": ["Maria"], "family": "Lopez"}],
            "birthDate": "1991-02-14",
        },
    ]
    for service in clinic.referral_services:
        resources.append(
            {
                "resourceType": "ServiceRequest",
                "id": f"referral-{service}-demo",
                "status": "active",
                "intent": "order",
                "subject": {"reference": "Patient/maria-demo"},
                "code": {"coding": [{"system": f"{SYSTEM}/service", "code": service}]},
            }
        )
    if clinic.accepted_payers:
        payer = clinic.accepted_payers[0]
        payer_id = f"demo-payer-{clinic.id}"
        resources.extend(
            [
                {"resourceType": "Organization", "id": payer_id, "name": payer},
                {
                    "resourceType": "Coverage",
                    "id": f"demo-coverage-{clinic.id}",
                    "status": "active",
                    "beneficiary": {"reference": "Patient/maria-demo"},
                    "payor": [{"reference": f"Organization/{payer_id}", "display": payer}],
                },
            ]
        )
    for provider, name in clinic.providers.items():
        schedule = f"schedule-{provider}"
        resources.extend(
            [
                {"resourceType": "Practitioner", "id": provider, "name": [{"text": name}]},
                {
                    "resourceType": "Schedule",
                    "id": schedule,
                    "active": True,
                    "actor": [{"reference": f"Practitioner/{provider}"}],
                },
            ]
        )
    resources.extend(synthetic_slots(clinic, now))
    # The HTTP seeder writes batches in order. Publish readiness after the fixture data.
    resources.append(
        {
            "resourceType": "Organization",
            "id": f"switchboard-{clinic.id}",
            "name": f"{clinic.name} (Switchboard synthetic fixtures)",
            "active": True,
        }
    )
    return resources
