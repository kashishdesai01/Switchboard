import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class Clinic:
    id: str
    version: int
    name: str
    timezone: ZoneInfo
    providers: dict[str, str]
    accepted_payers: tuple[str, ...]
    weekday_hours: str
    hold_seconds: int
    cancel_notice_hours: int
    locations: dict[str, str] = field(default_factory=dict)
    provider_locations: dict[str, str] = field(default_factory=dict)
    referral_services: dict[str, list[str]] = field(default_factory=dict)
    service_providers: dict[str, list[str]] = field(default_factory=dict)
    daily_demo_slots: bool = False
    preserve_demo_slots_for: tuple[str, ...] = ()


class ClinicFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    timezone: str
    providers: dict[str, str] = Field(min_length=1)
    accepted_payers: list[str]
    weekday_hours: str
    hold_seconds: int = Field(ge=30, le=600)
    cancel_notice_hours: int = Field(ge=0, le=168)
    locations: dict[str, str] = Field(default_factory=dict)
    provider_locations: dict[str, str] = Field(default_factory=dict)
    referral_services: dict[str, list[str]] = Field(default_factory=dict)
    service_providers: dict[str, list[str]] = Field(default_factory=dict)
    daily_demo_slots: bool = False
    preserve_demo_slots_for: list[str] = Field(default_factory=list)

    @field_validator("providers")
    @classmethod
    def valid_provider_keys(cls, providers: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[a-z0-9-]{1,20}", key) for key in providers):
            raise ValueError("Provider keys must be short lowercase FHIR IDs")
        return providers


def load_clinic(path: Path) -> Clinic:
    with path.open("rb") as file:
        config = ClinicFile.model_validate(tomllib.load(file))
    if set(config.provider_locations) - config.providers.keys() or (
        set(config.provider_locations.values()) - config.locations.keys()
    ):
        raise ValueError("Provider locations must reference configured providers and locations")
    if set(config.service_providers) - config.referral_services.keys() or any(
        not providers or set(providers) - config.providers.keys()
        for providers in config.service_providers.values()
    ):
        raise ValueError("Service providers must reference a configured service and provider")
    if set(config.preserve_demo_slots_for) - config.providers.keys():
        raise ValueError("Preserved demo schedules must reference configured providers")
    return Clinic(
        config.id,
        config.version,
        config.name,
        ZoneInfo(config.timezone),
        config.providers,
        tuple(config.accepted_payers),
        config.weekday_hours,
        config.hold_seconds,
        config.cancel_notice_hours,
        config.locations,
        config.provider_locations,
        config.referral_services,
        config.service_providers,
        config.daily_demo_slots,
        tuple(config.preserve_demo_slots_for),
    )


DEFAULT = Path(__file__).resolve().parent.parent / "clinics" / "bayview.toml"
BAYVIEW = load_clinic(DEFAULT)
VISIT_MINUTES = {"first visit": 40, "follow-up": 20}
HORIZON_DAYS = 60


def configured_clinic() -> Clinic:
    path = os.getenv("SWITCHBOARD_CLINIC_FILE")
    return load_clinic(Path(path)) if path else BAYVIEW
