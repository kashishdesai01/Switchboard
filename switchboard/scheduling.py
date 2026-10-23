import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from unicodedata import normalize
from uuid import uuid4

from switchboard.config import HORIZON_DAYS, VISIT_MINUTES, Clinic
from switchboard.store import (
    Conflict,
    DemoStore,
    FhirStore,
    Resource,
    ResourceStore,
    Unavailable,
    bundle_patient_reference,
)

SYSTEM = "https://switchboard.example/fhir"
HOLD_URL = f"{SYSTEM}/StructureDefinition/slot-hold"
RECEIPT_SYSTEM = f"{SYSTEM}/identifier/operation"
LOCATION_URL = f"{SYSTEM}/StructureDefinition/appointment-location"


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _name_key(text: str) -> str:
    text = normalize("NFKC", text).casefold().replace("’", "'").replace(".", "")
    return " ".join(text.split())


def hold_metadata(slot: Resource) -> tuple[str, datetime] | None:
    for extension in slot.get("extension", []):
        if extension["url"] == HOLD_URL:
            fields = {item["url"]: item for item in extension["extension"]}
            return fields["operation"]["valueString"], instant(fields["expires"]["valueInstant"])
    return None


def set_slot(slot: Resource, status: str) -> Resource:
    return {
        **slot,
        "status": status,
        "extension": [entry for entry in slot.get("extension", []) if entry["url"] != HOLD_URL],
    }


def uses_patient(appointment: Resource, patient: str) -> bool:
    return any(
        entry.get("actor", {}).get("reference") == f"Patient/{patient}"
        for entry in appointment.get("participant", [])
    )


@dataclass(frozen=True)
class Hold:
    operation: str
    slot: Resource
    expires: datetime


@dataclass(frozen=True)
class Change:
    operation: str
    action: str
    patient: str
    old: Resource | None
    hold: Hold | None
    new_patient: Resource | None = None


class Scheduling:
    def __init__(self, store: ResourceStore, clinic: Clinic, now: Callable[[], datetime] = utc_now):
        self.store = store
        self.clinic = clinic
        self.now = now
        self.registration_lock = asyncio.Lock()

    async def _matching_patients(self, full_name: str, dob: date) -> list[Resource]:
        if isinstance(self.store, DemoStore):
            name_key = _name_key(full_name)
            records = await self.store.search("Patient", birthdate=dob.isoformat())
            return [
                item
                for item in records
                if any(
                    _name_key(
                        name.get("text")
                        or " ".join([*name.get("given", []), name.get("family", "")])
                    )
                    == name_key
                    for name in item.get("name", [])
                )
            ]
        parts = " ".join(full_name.split()).casefold().split()
        if len(parts) == 1:
            lookups = [{"given": parts[0]}]
        else:
            lookups = [{"family": family} for family in sorted({parts[-1], " ".join(parts[1:])})]
        matches = {}
        for lookup in lookups:
            records = await self.store.search("Patient", birthdate=dob.isoformat(), **lookup)
            for item in records:
                if item.get("birthDate") == dob.isoformat() and any(
                    _name_key(
                        name.get("text")
                        or " ".join([*name.get("given", []), name.get("family", "")])
                    )
                    == _name_key(full_name)
                    for name in item.get("name", [])
                ):
                    matches[item["id"]] = item
        return list(matches.values())

    async def identify(self, full_name: str, dob: date) -> str | None:
        matches = {
            item["id"]
            for item in await self._matching_patients(full_name, dob)
            if item.get("active", True)
        }
        return next(iter(matches)) if len(matches) == 1 else None

    async def draft_demo_patient(self, full_name: str, dob: date) -> Resource | None:
        if not isinstance(self.store, (DemoStore, FhirStore)) or await self._matching_patients(
            full_name, dob
        ):
            return None
        given, separator, family = full_name.rpartition(" ")
        name = {"text": full_name, "given": given.split() if separator else [full_name]}
        if separator:
            name["family"] = family
        return {
            "resourceType": "Patient",
            "id": uuid4().hex,
            "active": True,
            "name": [name],
            "birthDate": dob.isoformat(),
        }

    async def options(
        self,
        visit: str,
        day: date | None = None,
        provider: str | None = None,
        period: str | None = None,
        week: date | None = None,
        requested_time: time | None = None,
        *,
        location: str | None = None,
        service: str | None = None,
        after: time | None = None,
        before: time | None = None,
        exclude: set[str] | None = None,
        days: tuple[date, ...] = (),
    ) -> list[Resource]:
        now = self.now()
        slots = await self.store.search("Slot", status="free", start=f"ge{now.isoformat()}")
        schedules = {f"Schedule/schedule-{key}" for key in self.clinic.providers}

        def in_window(slot: Resource) -> bool:
            start = instant(slot["start"]).astimezone(self.clinic.timezone)
            if day is not None and start.date() != day:
                return False
            if days and start.date() not in days:
                return False
            if week is not None and not week <= start.date() < week + timedelta(days=7):
                return False
            if after is not None and start.time() < after:
                return False
            if before is not None and start.time() > before:
                return False
            return period is None or (start.hour < 12) == (period == "morning")

        eligible = self.clinic.service_providers.get(service, self.clinic.providers)
        schedules &= {
            f"Schedule/schedule-{key}"
            for key in eligible
            if location is None or self.clinic.provider_locations.get(key) == location
        }

        def rank(slot: Resource) -> tuple[int, datetime, str]:
            start = instant(slot["start"])
            local = start.astimezone(self.clinic.timezone)
            distance = (
                abs((local.hour - requested_time.hour) * 60 + local.minute - requested_time.minute)
                if requested_time is not None
                else 0
            )
            return distance, start, slot["id"]

        latest_day = now.astimezone(self.clinic.timezone).date() + timedelta(days=HORIZON_DAYS)
        ranked = sorted(
            (
                slot
                for slot in slots
                if (exclude is None or slot["id"] not in exclude)
                and instant(slot["start"]) > now
                and instant(slot["start"]).astimezone(self.clinic.timezone).date() <= latest_day
                and instant(slot["end"]) - instant(slot["start"])
                == timedelta(minutes=VISIT_MINUTES[visit])
                and slot["schedule"]["reference"] in schedules
                and in_window(slot)
                and (
                    provider is None
                    or slot["schedule"]["reference"] == f"Schedule/schedule-{provider}"
                )
            ),
            key=rank,
        )
        if len(days) > 1:
            # Include an opening from each requested day before filling the remaining places.
            first_by_day = {}
            for slot in ranked:
                first_by_day.setdefault(
                    instant(slot["start"]).astimezone(self.clinic.timezone).date(), slot
                )
            spread = list(first_by_day.values())[:3]
            ids = {slot["id"] for slot in spread}
            return sorted(
                (spread + [slot for slot in ranked if slot["id"] not in ids])[:3], key=rank
            )
        if not self.clinic.daily_demo_slots or requested_time is not None:
            return ranked[:3]

        # If the caller is flexible, prefer different times over three versions of
        # the same time. Keep simultaneous openings as a fallback on a sparse day.
        distinct = []
        seen = set()
        for slot in ranked:
            start = instant(slot["start"])
            if start not in seen:
                seen.add(start)
                distinct.append(slot)
                if len(distinct) == 3:
                    return distinct
        chosen_ids = {slot["id"] for slot in distinct}
        return (distinct + [slot for slot in ranked if slot["id"] not in chosen_ids])[:3]

    def provider_for(self, resource: Resource) -> str:
        if "schedule" in resource:
            return resource["schedule"]["reference"].removeprefix("Schedule/schedule-")
        return next(
            (
                entry["actor"]["reference"].removeprefix("Practitioner/")
                for entry in resource.get("participant", [])
                if entry.get("actor", {}).get("reference", "").startswith("Practitioner/")
            ),
            "",
        )

    def location_for(self, resource: Resource) -> str | None:
        for extension in resource.get("extension", []):
            if extension.get("url") == LOCATION_URL:
                return extension.get("valueCode")
        return self.clinic.provider_locations.get(self.provider_for(resource))

    async def has_referral(self, patient: str, service: str) -> bool:
        ref = f"Patient/{patient}"
        requests = await self.store.search("ServiceRequest", subject=ref, status="active")
        return any(
            item.get("subject", {}).get("reference") == ref
            and item.get("status") == "active"
            and item.get("intent") == "order"
            and any(
                code.get("system") == f"{SYSTEM}/service" and code.get("code") == service
                for code in item.get("code", {}).get("coding", [])
            )
            for item in requests
        )

    async def email_on_file(self, patient: str) -> str | None:
        record = await self.store.read("Patient", patient)
        if not record or not record.get("active", True):
            return None
        addresses = {
            item.get("value")
            for item in record.get("telecom", [])
            if item.get("system") == "email" and not item.get("period") and item.get("value")
        }
        return next(iter(addresses)) if len(addresses) == 1 else None

    async def appointments(self, patient: str) -> list[Resource]:
        records = await self.store.search(
            "Appointment", patient=f"Patient/{patient}", status="booked"
        )
        return sorted(
            (
                item
                for item in records
                if uses_patient(item, patient) and instant(item["start"]) > self.now()
            ),
            key=lambda item: instant(item["start"]),
        )[:2]

    async def coverage(self, patient: str) -> tuple[str, ...]:
        reference = f"Patient/{patient}"
        records = await self.store.search("Coverage", beneficiary=reference, status="active")
        return tuple(
            sorted(
                {
                    payer["display"]
                    for record in records
                    if record.get("beneficiary", {}).get("reference") == reference
                    for payer in record.get("payor", [])
                    if payer.get("display")
                }
            )
        )[:2]

    async def hold(self, offered: Resource) -> Hold:
        if offered["status"] != "free" or instant(offered["start"]) <= self.now():
            raise Conflict("Slot unavailable")
        hold = Hold(uuid4().hex, offered, self.now() + timedelta(seconds=self.clinic.hold_seconds))
        slot = set_slot(offered, "busy-tentative")
        slot["extension"].append(
            {
                "url": HOLD_URL,
                "extension": [
                    {"url": "operation", "valueString": hold.operation},
                    {"url": "expires", "valueInstant": hold.expires.isoformat()},
                ],
            }
        )
        try:
            await self.store.transaction(updates=[slot], creates=[])
        except Unavailable:
            actual = await self.store.read("Slot", offered["id"])
            if not actual or hold_metadata(actual) != (hold.operation, hold.expires):
                raise
        return hold

    async def _release(self, slot: Resource) -> None:
        try:
            await self.store.transaction(updates=[set_slot(slot, "free")], creates=[])
        except Conflict:
            # A concurrent commit or sweeper already advanced this resource.
            pass

    async def release(self, hold: Hold) -> None:
        slot = await self.store.read("Slot", hold.slot["id"])
        if slot and slot["status"] == "busy-tentative":
            metadata = hold_metadata(slot)
            if metadata and metadata[0] == hold.operation:
                await self._release(slot)

    async def expire_holds(self) -> int:
        slots = await self.store.search("Slot", status="busy-tentative")
        expired = [
            slot
            for slot in slots
            if (metadata := hold_metadata(slot)) is not None and metadata[1] <= self.now()
        ]
        for slot in expired:
            await self._release(slot)
        return len(expired)

    async def receipt(self, operation: str) -> bool:
        records = await self.store.search("Basic", identifier=f"{RECEIPT_SYSTEM}|{operation}")
        return any(
            item.get("system") == RECEIPT_SYSTEM and item.get("value") == operation
            for record in records
            for item in record.get("identifier", [])
        )

    async def booked_for_operation(self, operation: str) -> bool:
        appointments = await self.store.search(
            "Appointment", identifier=f"{RECEIPT_SYSTEM}|{operation}", status="booked"
        )
        return any(
            item.get("status") == "booked"
            and any(
                identifier.get("system") == RECEIPT_SYSTEM and identifier.get("value") == operation
                for identifier in item.get("identifier", [])
            )
            for item in appointments
        )

    async def commit(self, change: Change) -> None:
        if change.new_patient is not None:
            if not isinstance(self.store, (DemoStore, FhirStore)) or change.action != "book":
                raise ValueError("New patients require a booking in a configured synthetic store")
            async with self.registration_lock:
                patient = change.new_patient
                matches = await self._matching_patients(
                    patient["name"][0]["text"], date.fromisoformat(patient["birthDate"])
                )
                if len(matches) == 1 and matches[0].get("active", True):
                    change = replace(change, patient=matches[0]["id"], new_patient=None)
                elif matches:
                    raise Conflict("Patient details are ambiguous")
                await self._commit(change)
            return
        await self._commit(change)

    async def _commit(self, change: Change) -> None:
        if await self.receipt(change.operation):
            return
        try:
            await self._commit_once(change)
        except (Conflict, Unavailable):
            # A sibling request may have won, or FHIR may have committed before its
            # response was lost. The immutable receipt commits in the same bundle.
            for delay in (0, 0.1, 0.25):
                if delay:
                    await asyncio.sleep(delay)
                if await self.receipt(change.operation):
                    return
            raise

    async def _commit_once(self, change: Change) -> None:
        updates = []
        creates = [
            {
                "resourceType": "Basic",
                "identifier": [{"system": RECEIPT_SYSTEM, "value": change.operation}],
                "code": {"coding": [{"system": f"{SYSTEM}/operation", "code": change.action}]},
            }
        ]
        if change.new_patient is not None:
            creates.append(change.new_patient)
        if change.old is not None:
            current = await self.store.read("Appointment", change.old["id"])
            if (
                not current
                or not uses_patient(current, change.patient)
                or current["status"] != "booked"
                or current["meta"]["versionId"] != change.old["meta"]["versionId"]
                or not self.can_change(current)
            ):
                raise Conflict("Appointment changed or unavailable")
            old_slots = current.get("slot", [])
            if len(old_slots) != 1:
                raise Conflict("Appointment slot unavailable")
            old_slot_ref = old_slots[0].get("reference", "")
            if not old_slot_ref.startswith("Slot/"):
                raise Conflict("Appointment slot unavailable")
            old_slot = await self.store.read("Slot", old_slot_ref.removeprefix("Slot/"))
            if (
                not old_slot
                or old_slot["status"] != "busy"
                or instant(old_slot["start"]) != instant(current["start"])
                or instant(old_slot["end"]) != instant(current["end"])
            ):
                raise Conflict("Appointment slot unavailable")
            updates.extend([{**current, "status": "cancelled"}, set_slot(old_slot, "free")])
        if change.hold is not None:
            hold = change.hold
            slot = await self.store.read("Slot", hold.slot["id"])
            if (
                not slot
                or slot["status"] != "busy-tentative"
                or hold_metadata(slot) != (hold.operation, hold.expires)
                or hold.expires <= self.now()
                or instant(slot["start"]) <= self.now()
                or any(slot[key] != hold.slot[key] for key in ("start", "end", "schedule"))
            ):
                raise Conflict("Hold expired or changed")
            provider = slot["schedule"]["reference"].removeprefix("Schedule/schedule-")
            patient_reference = (
                bundle_patient_reference(change.patient)
                if change.new_patient is not None and isinstance(self.store, FhirStore)
                else f"Patient/{change.patient}"
            )
            appointment = {
                "resourceType": "Appointment",
                "identifier": [{"system": RECEIPT_SYSTEM, "value": change.operation}],
                "status": "booked",
                "start": slot["start"],
                "end": slot["end"],
                "slot": [{"reference": f"Slot/{slot['id']}"}],
                "participant": [
                    {"actor": {"reference": ref}, "status": "accepted"}
                    for ref in (patient_reference, f"Practitioner/{provider}")
                ],
            }
            if location := self.location_for(slot):
                appointment["extension"] = [{"url": LOCATION_URL, "valueCode": location}]
            creates.append(appointment)
            updates.append(set_slot(slot, "busy"))
        await self.store.transaction(updates=updates, creates=creates)

    def can_change(self, appointment: Resource) -> bool:
        return instant(appointment["start"]) >= self.now() + timedelta(
            hours=self.clinic.cancel_notice_hours
        )
