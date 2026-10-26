from __future__ import annotations

import logging
import re
import sqlite3
from calendar import month_abbr, month_name
from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from time import monotonic
from typing import TYPE_CHECKING
from unicodedata import category, normalize
from uuid import uuid4

from switchboard.frontdesk import email_address
from switchboard.intents import Route, Router
from switchboard.scheduling import Change, Scheduling, instant
from switchboard.store import Conflict, Resource, Unavailable

if TYPE_CHECKING:
    from switchboard.frontdesk import FrontDesk
    from switchboard.operations import Trace

LOG = logging.getLogger(__name__)

EMERGENCY_SCRIPT = "If this is a medical emergency, hang up and call 911 now."
EMERGENCY = re.compile(
    r"\b(chest pain|can(?:not|'t) breathe|difficulty breathing|"
    r"bleeding (?:a lot|heavily)|overdos\w*|suicid\w*|"
    r"(?:hurt|kill) myself|stroke|unconscious)\b",
    re.I,
)
STAFF = re.compile(
    r"\b(?:human|representative|front desk|refill|test results|medical advice|billing|"
    r"medication|symptoms?|dosage|should i take|is it normal)\b",
    re.I,
)
PROXY = re.compile(r"\b(?:for|on behalf of) my (?:child|son|daughter|parent|wife|husband)\b", re.I)
AFFIRMATIVE = r"(?:yes|yeah|yep|yup|ya|sure|absolutely|of course|okay|ok|right|correct|confirm|sounds good|go (?:right )?ahead|that(?:'s| is) (?:correct|right|fine))"
COURTESY = r"(?:please|thanks|thank you)"
CONFIRM_ACTION = {"book": r"book|schedule", "reschedule": r"reschedule|move", "cancel": r"cancel"}
NAME_INTRO = re.compile(
    r"^(?:(?:my |the )?(?:full )?name is|i am|i['’]m|it['’]s|this is)\b[ ,:]*", re.I
)
PROVIDER_TITLE = re.compile(
    r"\b(?:(?:the|a|any|my|our|your)\s+doctor\b|(?:dr\.?|doctor)\s+([^\W\d_]+))", re.I
)
PROVIDER_NON_NAMES = frozenset(
    "today tomorrow tonight this next now later soon again on at in around before after "
    "monday tuesday wednesday thursday friday saturday sunday please is can available appointment".split()
)
INTRODUCED_NAME = re.compile(r"\bmy (?:full )?name is\s+([^!?;,]+)", re.I)
SENTENCE_AS_NAME = re.compile(
    r"\b(?:i|we|you|it|that|this|they|he|she)\s+(?:am|are|is|was|were|can|could|do|did|need|want|will|would|should|have|has)\b"
    r"|\b(?:could you|please repeat|my name|not sure|i'm|i've)\b",
    re.I,
)
MORE_TIMES = re.compile(
    r"\b(?:(?:other|more|different|additional) (?:times|openings|slots|options)|what else (?:is available|do you have))\b"
)
MONTHS = {
    label.casefold(): index
    for names in (month_name, month_abbr)
    for index, label in enumerate(names)
    if index
}
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))


def answer_to_confirmation(words: str, action: str | None = None) -> str | None:
    # Match complete replies so a qualifier such as "but on Monday" reaches scheduling.
    reply = " ".join(re.sub(r"[.!?,;]+", " ", words).split())
    if re.fullmatch(rf"(?:(?:ok|okay) )?(?:no|nope|no way|don't|do not)(?: {COURTESY})?", reply):
        return "no"
    reply = re.sub(r"^(yes|yeah|ya|yep) shore\b", r"\1 sure", reply)
    if action and reply in {"ok", "okay", "right"}:
        return None
    if re.fullmatch(rf"(?:{AFFIRMATIVE} )*{AFFIRMATIVE}(?: {COURTESY})?", reply):
        return "yes"
    if action and re.fullmatch(
        rf"(?:(?:{AFFIRMATIVE}|please) )*(?:{CONFIRM_ACTION[action]}) "
        rf"(?:it|that|the appointment)(?: {COURTESY})?",
        reply,
    ):
        return "yes"
    return None


@dataclass(frozen=True)
class Reply:
    text: str
    id: str
    outcome: str | None = None
    confirmation: bool = False
    calendar: tuple[dict, ...] = ()
    email_status: str | None = None


class Call:
    """A transport serializes input and reports finished playback by reply ID."""

    def __init__(
        self,
        scheduling: Scheduling,
        router: Router | None = None,
        trace: Trace | None = None,
        *,
        allow_registration: bool = False,
        frontdesk: FrontDesk | None = None,
        email_form: bool = False,
    ):
        self.trace = trace
        self.scheduling = scheduling
        self.router = router
        self.allow_registration = allow_registration
        self.frontdesk = frontdesk
        self.email_form = email_form
        self.stage = "intent"
        self.patient: str | None = None
        self.name = ""
        self.dob: date | None = None
        self.dob_fragment: str | None = None
        self.dob_retries = 0
        self.new_patient: Resource | None = None
        self.attempts = 0
        self.intent = "book"
        self.visit = "follow-up"
        self.day: date | None = None
        self.days: tuple[date, ...] = ()
        self.week: date | None = None
        self.period: str | None = None
        self.requested_time: time | None = None
        self.after: time | None = None
        self.before: time | None = None
        self.location: str | None = None
        self.service: str | None = None
        self.last_operation: str | None = None
        self.email_on_file: str | None = None
        self.email_checked = False
        self.pending_email: str | None = None
        self.staff_topic: str | None = None
        self.unclear = 0
        self.digits = ""
        self.provider: str | None = None
        self.options: list[Resource] = []
        self.shown_slots: set[str] = set()
        self.old: Resource | None = None
        self.change: Change | None = None
        self.prompt_id: str | None = None
        self.played = False

    def greeting(self) -> Reply:
        return self.say(
            f"Hi, thanks for calling {self.scheduling.clinic.name}. I'm the automated demo assistant. "
            "How can I help today?"
        )

    def say(
        self,
        text: str,
        *,
        outcome: str | None = None,
        confirm: bool = False,
        calendar: tuple[dict, ...] = (),
        email_status: str | None = None,
    ) -> Reply:
        reply = Reply(text, uuid4().hex, outcome, confirm, calendar, email_status)
        if self.trace:
            self.trace.record("assistant", text)
        self.prompt_id, self.played = (reply.id if confirm else None), False
        if outcome:
            if self.trace and (self.trace.outcome is None or outcome == "emergency"):
                self.trace.outcome = outcome
                if (
                    self.frontdesk
                    and outcome in {"handoff", "needs_review", "emergency"}
                    and not self.trace.work
                ):
                    reason = {
                        "handoff": "policy_review",
                        "needs_review": "appointment_write_uncertain",
                        "emergency": "emergency",
                    }[outcome]
                    self._work(reason)
            self.stage = "closed"
        return reply

    def _work(self, reason: str) -> None:
        if self.trace and self.frontdesk:
            context = {"request": self.intent, "stage": self.stage}
            if self.patient and not self.new_patient:
                context["patient_reference"] = f"Patient/{self.patient}"
            context.update({key: value for key, value in self._preferences().items() if value})
            if self.last_operation:
                context["operation"] = self.last_operation
            if self.staff_topic:
                context["topic"] = self.staff_topic
            self.trace.work = reason, context

    def _handoff(self, reason: str, message: str) -> Reply:
        self._work(reason)
        return self.say(message, outcome="handoff")

    def playback_finished(self, reply_id: str | None) -> None:
        if reply_id and reply_id == self.prompt_id:
            self.played = True

    def interrupt(self) -> None:
        self.prompt_id = None
        self.played = False

    async def close(self) -> None:
        change, self.change = self.change, None
        if change and change.hold:
            try:
                await self.scheduling.release(change.hold)
            except Unavailable:
                # The authoritative FHIR hold contains its own expiry.
                pass

    def describe(self, item: Resource) -> str:
        start = instant(item["start"]).astimezone(self.scheduling.clinic.timezone)
        clinic = self.scheduling.clinic
        name = clinic.providers.get(self.scheduling.provider_for(item), "the clinic")
        location = (
            clinic.locations.get(self.scheduling.location_for(item)) if self.frontdesk else None
        )
        return f"{start:%A, %B} {start.day} at {start:%I:%M %p} with {name}" + (
            f" at {location}" if location else ""
        )

    def _calendar(self, items: list[Resource], state: str) -> tuple[dict, ...]:
        if not self.frontdesk:
            return ()
        return tuple(
            {
                "start": item["start"],
                "end": item["end"],
                "label": self.describe(item),
                "state": state,
            }
            for item in items
        )

    async def handle(self, text: str) -> Reply:
        stage = self.stage
        if self.trace:
            self.trace.record("caller", text)
        started = monotonic()
        try:
            return await self._handle_turn(text)
        finally:
            if self.trace:
                self.trace.add(
                    "turn_processed",
                    state=f"{stage} → {self.stage}",
                    latency_ms=round((monotonic() - started) * 1000),
                )

    async def _handle_turn(self, text: str) -> Reply:
        text = text.strip()
        words = " ".join(text.lower().replace(",", " ").replace("’", "'").strip(".!? ").split())
        if EMERGENCY.search(words):
            reply = self.say(EMERGENCY_SCRIPT, outcome="emergency")
            if self.trace:
                self.trace.add("emergency_routed")
            return reply
        if self.stage == "closed":
            return self.say("This call has ended.", outcome="closed")
        staff = STAFF.search(words)
        proxy = PROXY.search(words)
        if words == "0" or staff or proxy:
            self.staff_topic = (
                "proxy request" if proxy else staff[0] if staff else "staff requested"
            )
            await self.close()
            return self._handoff(
                "staff_requested",
                "Of course. I'll pass this request to the front desk."
                if self.frontdesk
                else "This request needs the front desk.",
            )
        try:
            try:
                return await self._handle(text, words)
            except Conflict:
                await self.close()
                if self.intent == "book":
                    return await self._offer(prefix="That time is no longer available. ")
                return self.say(
                    "The appointment changed. Please contact the front desk.", outcome="handoff"
                )
        except Unavailable:
            await self.close()
            return self.say(
                "I couldn't verify the outcome. Please check with the front desk before trying again.",
                outcome="needs_review",
            )

    async def _handle(self, text: str, words: str) -> Reply:
        answer = answer_to_confirmation(words)
        match self.stage:
            case "intent":
                return await self._start(words, text)
            case "other_provider":
                self.provider = self._provider(words)
                if self.provider or answer == "yes" or words in {"anyone", "another doctor"}:
                    self._set_window(words)
                    return self._ask_name()
                if answer == "no":
                    return self.say("Of course. Thanks for calling.", outcome="declined")
                return self.say("Would you like to hear times with another doctor here?")
            case "name":
                return self._capture_name(text)
            case "check_name":
                if answer == "yes" and self.played:
                    self.stage = "dob"
                    return self.say("Thanks. What's your date of birth?")
                if answer == "no" or words in {"change it", "change my name", "the name"}:
                    self.name = ""
                    return self._ask_name()
                if INTRODUCED_NAME.search(text):
                    return self._capture_name(text, confirm=True)
                return self._ask_name()
            case "day_alternative":
                if answer == "yes" or MORE_TIMES.search(words):
                    self.day = None
                    self.days = ()
                    return await self._offer()
                if answer == "no":
                    return self.say("Of course. I haven't booked anything.", outcome="declined")
                if await self._update_preferences(words):
                    return await self._offer()
                return self.say(
                    "Would you like the next available day, or is there another day you prefer?"
                )
            case "dob":
                if words in {"keypad", "use the keypad"}:
                    self.stage = "keypad"
                    self.digits = ""
                    return self.say("Enter eight digits in year, month, day order.")
                dates = date_candidates(text)
                if len(dates) > 1:
                    self.dob_fragment = None
                    return self.say(
                        "I heard two different birth dates. Could you say your birth date once?"
                    )
                self.dob = next(iter(dates), None)
                if self.dob is None and self.dob_fragment:
                    year = re.fullmatch(r"(?:i was born in |in )?(\d{4})", _date_words(text))
                    if year:
                        self.dob = parse_date(f"{self.dob_fragment} {year[1]}")
                if self.dob is None:
                    fragment = re.search(
                        rf"\b(?:\d{{1,2}}\s+(?:(?:of|to)\s+)?(?:{MONTH_PATTERN})|"
                        rf"(?:{MONTH_PATTERN})\s+\d{{1,2}})\b",
                        _date_words(text),
                    )
                    if fragment and not re.search(r"\b\d{4}\b", text):
                        self.dob_fragment = fragment[0].replace(" to ", " of ")
                        return self.say("I caught the month and day. What year were you born?")
                if self.dob is None or self.dob > self.scheduling.now().date():
                    self.dob = None
                    self.dob_retries += 1
                    if self.dob_retries >= 2:
                        return self.say(
                            "I'm having trouble hearing the date. Say it again, or say use the keypad."
                        )
                    if self.dob_fragment:
                        return self.say("I have the month and day. What year were you born?")
                    return self.say("Could you repeat your birth date, including the year?")
                self.dob_fragment = None
                self.dob_retries = 0
                self.stage = "check_dob"
                return self._read_identity()
            case "keypad":
                return self.say("Enter eight digits on the keypad, or press zero twice for staff.")
            case "check_dob":
                if answer == "no":
                    self.stage = "correct_identity"
                    return self.say("Of course. Which should I change, the name or date of birth?")
                if answer != "yes":
                    return self._correct_identity(text, words)
                return await self._verify()
            case "correct_identity":
                return self._correct_identity(text, words)
            case "select_existing":
                return await self._select_existing(words)
            case "select_slot":
                option = self._selection(words) or self._offered_time(text, words)
                if option is None:
                    if self.frontdesk and re.search(
                        r"\b(?:join|add me to|put me on) (?:the )?wait ?list\b", words
                    ):
                        return await self._join_waitlist()
                    if self.frontdesk and (answer := self._scheduling_question(words)):
                        return self._list_options(answer)
                    if await self._update_preferences(words):
                        return await self._offer()
                    if MORE_TIMES.search(words):
                        return await self._offer(more=True)
                    if self.frontdesk and (answer == "yes" or words in {"i see", "got it"}):
                        return self.say(
                            "Take your time. Which of those options would you prefer?",
                            calendar=self._calendar(self.options, "offered"),
                        )
                    if self.frontdesk:
                        self.unclear += 1
                        if self.unclear >= 3:
                            return self._handoff(
                                "no_availability",
                                "I'll ask the front desk to help you choose a time.",
                            )
                        return self._list_options(
                            "Which option would you like? You can also give me a different day, time, or location."
                        )
                    return self._list_options("Please say the number of the time you'd like.")
                self.unclear = 0
                hold = await self.scheduling.hold(option)
                self.change = Change(
                    hold.operation, self.intent, self._patient(), self.old, hold, self.new_patient
                )
                return await self._prepare_confirmation()
            case "confirm_change":
                return await self._confirm(words)
            case "email_address":
                return self._capture_email(text, words)
            case "email_readback":
                if answer == "yes" and self.played:
                    self.email_on_file, self.pending_email = self.pending_email, None
                    self.stage = "confirm_change"
                    return self._read_change(
                        "Thank you. I'll use that address for the confirmation. "
                    )
                if answer == "no":
                    self.pending_email = None
                    self.stage = "email_address"
                    return self.say(
                        "Of course. What email address should I use? You can also say skip email."
                    )
                return self._capture_email(text, words)
            case _:
                raise RuntimeError(f"Invalid conversation stage: {self.stage}")

    async def digit(self, digit: str) -> Reply | None:
        if self.stage != "keypad":
            return await self.handle(digit) if digit in {"0", "1", "2", "3"} else None
        if not re.fullmatch(r"[0-9]", digit):
            return None
        self.digits += digit
        if self.digits == "00":
            self.digits = ""
            self.stage = "dob"
            return await self.handle("0")
        if len(self.digits) < 8:
            return None
        self.dob = parse_date(f"{self.digits[:4]}-{self.digits[4:6]}-{self.digits[6:]}")
        if self.trace:
            self.trace.record("caller", "[Date entered on keypad]")
        self.digits = ""
        if self.dob is None:
            return self.say("That date could not be read. Please enter it again.")
        self.stage = "check_dob"
        return self._read_identity()

    async def _route(self, words: str) -> Route:
        if re.search(r"\b(reschedule|move|change)\b", words):
            return "reschedule"
        if re.search(r"\bcancel\b", words):
            return "cancel"
        if re.search(r"\b(book|schedule)\b", words):
            return "book"
        if re.search(r"\b(insurance|coverage|accept)\b", words):
            return "insurance"
        if re.search(r"\bappointment\b", words):
            return "book"
        return (
            await self.router.classify(words)
            if self.router and not self._identity_details(words)
            else "unknown"
        )

    async def _start(self, words: str, text: str) -> Reply:
        if self.frontdesk and (opening_name := INTRODUCED_NAME.search(text)):
            candidate = self._name_candidate(opening_name[1])
            if candidate:
                self.name = candidate
        if (
            self.frontdesk
            and re.search(r"\b(locations?|where are you|addresses)\b", words)
            and not re.search(r"\b(book|schedule|appointment)\b", words)
        ):
            return self.say(self._locations(), outcome="info_only")
        if re.search(r"\b(hours|when are you open)\b", words):
            return self.say(
                f"The clinic is open {self.scheduling.clinic.weekday_hours}.", outcome="info_only"
            )
        if re.search(r"\bmy (insurance|coverage|plan)\b", words) and not re.search(
            r"\b(accept|take)\b", words
        ):
            self.intent = "coverage"
            return self._ask_name()
        intent = await self._route(words)
        if (
            self.frontdesk
            and intent == "unknown"
            and self._service(words)
            and re.search(r"\b(?:see|consultation|consult|visit|specialist)\b", words)
        ):
            intent = "book"
        if intent == "insurance":
            payers = self.scheduling.clinic.accepted_payers
            if any(
                re.search(rf"(?<!\w){re.escape(payer.casefold())}(?!\w)", words) for payer in payers
            ):
                return self.say(
                    "That payer is on the clinic list. Staff can confirm your plan and benefits.",
                    outcome="info_only",
                )
            return self.say(
                f"The clinic lists {', '.join(payers)}. Staff can confirm other plans.",
                outcome="info_only",
            )
        if intent == "staff":
            return self.say("This request needs the front desk.", outcome="handoff")
        if intent == "unknown":
            self.unclear += 1
            if self.frontdesk and self.unclear >= 3:
                return self._handoff(
                    "staff_requested", "I'll ask the front desk to help with your request."
                )
            return self.say(
                "I'm listening. Are you looking to book or change an appointment, or ask about insurance?"
            )
        self.intent = intent
        self.unclear = 0
        self.visit = (
            "first visit" if re.search(r"\b(first visit|new patient)\b", words) else "follow-up"
        )
        self.provider = self._provider(words)
        if self.frontdesk:
            self.service = self._service(words)
            if self.service and intent == "book":
                self.visit = "first visit"
        if self.frontdesk and (identity := self._identity_details(text)):
            name_intro = re.search(
                r"\b(?:my (?:full )?name is|this is)\b", text[: identity.start()], re.I
            )
            identity_start = name_intro.start() if name_intro else identity.start()
            preference_words = text[:identity_start].casefold()
        else:
            identity = None
            preference_words = words
        await self._update_preferences(preference_words)
        requested = next(
            (
                match[1]
                for match in PROVIDER_TITLE.finditer(words)
                if match[1] and match[1] not in PROVIDER_NON_NAMES
            ),
            None,
        )
        if self.provider is None and requested:
            self.stage = "other_provider"
            return self.say(
                f"Dr. {requested.title()} isn't on the schedule for "
                f"{self.scheduling.clinic.name}. Would you like to hear times with another doctor here?"
            )
        if identity:
            self.stage = "name"
            return self._capture_name(text[identity_start:])
        return self._ask_name()

    @staticmethod
    def _identity_details(text: str) -> re.Match | None:
        return re.search(r"\b(?:date of birth|birthday|born|dob)\b", text, re.I)

    def _service(self, words: str) -> str | None:
        return next(
            (
                service
                for service, aliases in self.scheduling.clinic.referral_services.items()
                if any(re.search(rf"\b{re.escape(alias)}\b", words) for alias in aliases)
            ),
            None,
        )

    def _locations(self) -> str:
        locations = ", ".join(self.scheduling.clinic.locations.values())
        return (
            f"We can schedule visits at {locations}."
            if locations
            else "The front desk can help with location details."
        )

    def _scheduling_question(self, words: str) -> str | None:
        if re.search(
            r"\b(?:what (?:are|is) your hours|when (?:do you open|are you open))\b", words
        ):
            return f"We're open {self.scheduling.clinic.weekday_hours}."
        if re.search(r"\b(?:where (?:is|are)|which location|what location)\b", words):
            return self._locations()
        if re.search(r"\b(?:how long|visit length)\b", words):
            return f"A {self.visit} is scheduled for {40 if self.visit == 'first visit' else 20} minutes."
        if re.search(
            r"\b(?:what (?:were|are) (?:my |the )?options|repeat (?:the |my )?(?:times|options))\b",
            words,
        ):
            return "Of course. Here are the options again."
        return None

    async def _update_preferences(self, words: str) -> bool:
        changed = self._set_window(words)
        if not self.frontdesk:
            return changed
        clinic = self.scheduling.clinic
        location = next(
            (
                key
                for key, name in clinic.locations.items()
                if any(
                    re.search(rf"\b{re.escape(alias)}\b", words) for alias in (key, name.casefold())
                )
            ),
            None,
        )
        provider = self._provider(words)
        if location:
            self.location = location
        if provider:
            self.provider = provider
        if re.search(r"\b(?:either|any) location\b", words):
            self.location = None
            changed = True
        if re.search(r"\b(?:any|either) (?:provider|doctor)\b", words):
            self.provider = None
            changed = True
        if re.search(r"\b(?:any day|any date|flexible on the date)\b", words):
            self.day = self.week = None
            self.days = ()
            changed = True
        if re.search(r"\b(?:any time|anytime|flexible on the time)\b", words):
            self.requested_time = self.after = self.before = self.period = None
            changed = True
        if changed or location or provider:
            return True
        if (
            self.router
            and not self._identity_details(words)
            and self.stage in {"intent", "select_slot", "confirm_change"}
            and re.search(
                r"\b(?:after|before|around|later|earlier|next|weekday|weekend|o'clock|noon)\b",
                words,
            )
        ):
            extract = getattr(self.router, "preferences", None)
            if extract and (normalized := await extract(words)):
                return self._set_window(normalized)
        return False

    def _preferences(self) -> dict:
        return {
            "visit": self.visit,
            "service": self.service,
            "provider": self.provider,
            "location": self.location,
            "day": self.day.isoformat() if self.day else None,
            "days": [day.isoformat() for day in self.days] or None,
            "week": self.week.isoformat() if self.week else None,
            "period": self.period,
            "after": self.after.isoformat(timespec="minutes") if self.after else None,
            "before": self.before.isoformat(timespec="minutes") if self.before else None,
            "requested_time": self.requested_time.isoformat(timespec="minutes")
            if self.requested_time
            else None,
        }

    async def _join_waitlist(self) -> Reply:
        if not self.patient or self.new_patient:
            return self._handoff(
                "no_availability",
                "I'll ask the front desk to help set up your patient record and discuss openings.",
            )
        if self.service and not await self.scheduling.has_referral(self.patient, self.service):
            return self._referral_handoff()
        assert self.frontdesk and self.trace
        criteria = self._preferences()
        if self.service:
            criteria["eligible_providers"] = self.scheduling.clinic.service_providers.get(
                self.service, []
            )
        await self.frontdesk.join(self.trace.id, self.patient, criteria)
        return self.say(
            "You're on the waitlist. Staff can follow up if a matching appointment opens.",
            outcome="waitlisted",
        )

    def _referral_handoff(self) -> Reply:
        return self._handoff(
            "referral_required",
            "This visit requires a referral on file. I'll ask the front desk to review it and help you with the next step.",
        )

    def _provider(self, words: str) -> str | None:
        return next(
            (
                key
                for key in self.scheduling.clinic.providers
                if re.search(rf"\b{re.escape(key)}\b", words)
            ),
            None,
        )

    def _ask_name(self) -> Reply:
        if self.name:
            self.stage = "check_name"
            return self.say(
                f"I heard your name as {self.name}. Is that correct? You can also say change my name.",
                confirm=True,
            )
        self.stage = "name"
        return self.say("What name should I use?")

    @staticmethod
    def _name_candidate(text: str) -> str | None:
        if introduction := INTRODUCED_NAME.search(text):
            text = introduction[1]
        text = NAME_INTRO.sub("", " ".join(text.split()))
        text = re.split(r"(?<!\b[^\W\d_])\.(?=\s|$)", text, maxsplit=1)[0]
        text = re.split(
            r"\s+(?:(?:and\s+)?(?:i(?:'m| am| want| need| would)|my date of birth|born))\b",
            text,
            maxsplit=1,
            flags=re.I,
        )[0]
        name = normalize("NFC", text.strip(" ,.!?"))
        if SENTENCE_AS_NAME.search(name) or answer_to_confirmation(name.casefold()):
            return None
        parts = name.split()
        if len(parts) >= 3 and all(len(part) == 1 and part.isalpha() for part in parts):
            name = "".join(parts).title()
        if not any(char.isalpha() for char in name) or any(
            char not in " ’'‐-.·ʻ" and category(char)[0] not in {"L", "M"} for char in name
        ):
            return None
        return name

    def _capture_name(self, text: str, *, confirm: bool = False) -> Reply:
        if self.frontdesk and (identity := self._identity_details(text)):
            supplied_dob = parse_date(text[identity.end() :])
            if supplied_dob and supplied_dob <= self.scheduling.now().date():
                self.dob = supplied_dob
            text = text[: identity.start()].rstrip(" ,.;")
            text = re.sub(r"\s+(?:and\s+)?my$", "", text, flags=re.I)
            if match := re.search(r"\b(?:my name is|this is)\b", text, re.I):
                text = text[match.start() :]
        name = self._name_candidate(text)
        if not name:
            return self.say(
                "I may have misheard your name. Could you repeat it or spell it for me?"
            )
        self.name = name
        if self.dob is not None:
            self.stage = "check_dob"
            return self._read_identity()
        if confirm:
            return self._ask_name()
        self.stage = "dob"
        return self.say("Thanks. What's your date of birth?")

    def _set_window(self, words: str) -> bool:
        today = self.scheduling.now().astimezone(self.scheduling.clinic.timezone).date()
        days = requested_days(words, today)
        day = days[0] if len(days) == 1 else None
        requested_time = parse_time(words)
        after = before = None
        if self.frontdesk:
            lower = re.search(r"\b(?:after|from)\s+(.+?)(?=\s+(?:and|before|until)\b|$)", words)
            upper = re.search(r"\b(?:before|until)\s+(.+?)(?=\s+(?:and|after|from)\b|$)", words)
            between = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+)", words)
            if between:
                after = parse_time(between[1], clinic_hours=True)
                before = parse_time(between[2], clinic_hours=True)
            else:
                after = parse_time(lower[1], clinic_hours=True) if lower else None
                before = parse_time(upper[1], clinic_hours=True) if upper else None
        week = None
        period_match = re.search(r"\b(morning|afternoon)\b", words)
        period = period_match[1] if period_match else None
        weekday = re.search(
            r"\b(next )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", words
        )
        if not days and re.search(r"\b(?:today|this (?:morning|afternoon))\b", words):
            day = today
        elif not days and "tomorrow" in words:
            day = today + timedelta(days=1)
        elif not days and weekday:
            weekdays = (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
            index = weekdays.index(weekday[2])
            delta = (
                7 - today.weekday() + index if weekday[1] else (index - today.weekday()) % 7 or 7
            )
            day = today + timedelta(days=delta)
        elif not days and "next week" in words:
            week = today + timedelta(days=7 - today.weekday())
        if (
            not days
            and day is None
            and week is None
            and period is None
            and requested_time is None
            and after is None
            and before is None
        ):
            return False
        if days or day is not None or week is not None:
            self.day, self.week = day, week
            self.days = days if len(days) > 1 else ()
        if after is not None or before is not None:
            self.requested_time = self.period = None
            self.after = after or self.after
            self.before = before or self.before
        elif requested_time is not None or period is not None:
            self.after = self.before = None
            self.requested_time, self.period = (
                requested_time,
                period if requested_time is None else None,
            )
        return True

    def _patient(self) -> str:
        if self.patient is None:
            raise RuntimeError("Patient has not been verified")
        return self.patient

    def _read_identity(self) -> Reply:
        assert self.dob is not None
        return self.say(
            f"I heard {self.name}, born {self.dob:%B} {self.dob.day}, {self.dob.year}. "
            "Is that correct?",
            confirm=True,
        )

    def _correct_identity(self, text: str, words: str) -> Reply:
        corrected_date = parse_date(text)
        if corrected_date and corrected_date <= self.scheduling.now().date():
            self.dob = corrected_date
            self.stage = "check_dob"
            return self._read_identity()
        if words in {"name", "my name", "the name", "change my name"}:
            self.stage = "name"
            return self.say("What name should I use?")
        if words in {"birthday", "birth date", "date of birth", "my date of birth"}:
            self.stage = "dob"
            self.dob = None
            return self.say("What's the correct date of birth?")
        if (
            INTRODUCED_NAME.search(text)
            or NAME_INTRO.match(text)
            or (self.stage == "correct_identity" and answer_to_confirmation(words) is None)
        ):
            return self._capture_name(text)
        return self._read_identity()

    async def _verify(self) -> Reply:
        if not self.played or self.dob is None:
            return self._read_identity()
        self.patient = await self.scheduling.identify(self.name, self.dob)
        if self.patient is None and self.intent == "book" and self.allow_registration:
            self.new_patient = await self.scheduling.draft_demo_patient(self.name, self.dob)
            if self.new_patient is not None:
                self.patient = self.new_patient["id"]
                self.visit = "first visit"
                self.name, self.dob = "", None
                return await self._offer(
                    "I can set up a new demo patient record with your booking. "
                )
        self.name, self.dob = "", None
        if self.patient is None:
            self.attempts += 1
            if self.attempts >= 2:
                return self._handoff(
                    "identity_unverified",
                    "I couldn't verify those details. Please contact staff.",
                )
            self.stage = "name"
            return self.say("I couldn't verify those details. Please repeat your full name.")
        if self.intent == "book":
            return await self._offer()
        if self.intent == "coverage":
            payers = await self.scheduling.coverage(self._patient())
            if payers:
                return self.say(
                    f"The clinic record lists {', '.join(payers)}. Staff can verify benefits.",
                    outcome="info_only",
                )
            return self.say(
                "I couldn't find an active coverage record. Staff can verify it.", outcome="handoff"
            )
        self.options = await self.scheduling.appointments(self._patient())
        if not self.options:
            return self.say("I found no upcoming appointments to change.", outcome="info_only")
        self.stage = "select_existing"
        return self._list_options("Which existing appointment do you want to change?")

    def _list_options(self, lead: str) -> Reply:
        options = " ".join(
            f"Option {index}: {self.describe(item)}." for index, item in enumerate(self.options, 1)
        )
        return self.say(
            f"{lead} {options}".strip(), calendar=self._calendar(self.options, "offered")
        )

    async def _offer(self, prefix: str = "", *, more: bool = False) -> Reply:
        self.unclear = 0
        if self.service and (
            self.new_patient
            or not await self.scheduling.has_referral(self._patient(), self.service)
        ):
            return self._referral_handoff()
        clinic = self.scheduling.clinic
        if (
            self.provider
            and self.location
            and clinic.provider_locations.get(self.provider) != self.location
        ):
            self.options = []
            self.stage = "select_slot"
            actual = clinic.locations.get(clinic.provider_locations.get(self.provider))
            if actual:
                return self.say(
                    f"{clinic.providers[self.provider]} sees patients at {actual}. "
                    "Would you prefer that location or a different provider?"
                )
        if (
            self.service
            and self.provider
            and self.provider not in clinic.service_providers.get(self.service, clinic.providers)
        ):
            self.options = []
            self.stage = "select_slot"
            return self.say(
                f"{clinic.providers[self.provider]} isn't scheduled for that visit type. "
                "Would you like to look for a different provider?"
            )
        if self.after and self.before and self.after > self.before:
            self.options = []
            self.stage = "select_slot"
            return self.say(
                "Could you confirm the time window? The earliest time is later than the latest time I heard."
            )
        if not more:
            self.shown_slots.clear()
        self.options = await self.scheduling.options(
            self.visit,
            self.day,
            self.provider,
            self.period,
            self.week,
            self.requested_time,
            location=self.location,
            service=self.service,
            after=self.after,
            before=self.before,
            exclude=self.shown_slots,
            days=self.days,
        )
        self.shown_slots.update(slot["id"] for slot in self.options)
        self.stage = "select_slot"
        if not self.options:
            if more:
                return self.say(
                    "I've shown all the openings that match. Would another day, time, or location work?"
                )
            if self.day == self.scheduling.now().astimezone(clinic.timezone).date():
                self.stage = "day_alternative"
                return self.say(
                    "I don't have an opening today. Would you like the next available day?"
                )
            if self.frontdesk:
                waitlist = " You can also ask to join the waitlist." if not self.new_patient else ""
                return self.say(
                    f"{prefix}I don't see an opening with those preferences. Would another day, time, or location work?{waitlist}"
                )
            if self.requested_time is not None:
                return self.say(
                    f"{prefix}I don't have an opening then. What other day or time would work?"
                )
            return self.say(
                f"{prefix}No matching times are available. Please contact staff.", outcome="handoff"
            )
        if self.requested_time is not None:
            first_start = instant(self.options[0]["start"]).astimezone(
                self.scheduling.clinic.timezone
            )
            requested = self.requested_time.strftime("%I:%M %p").lstrip("0")
            if first_start.time() != self.requested_time:
                lead = (
                    "Here are more nearby openings."
                    if more
                    else f"I don't have a {requested} opening. Here are the closest times I can offer."
                )
                return self._list_options(prefix + lead)
            if any(
                instant(slot["start"]).astimezone(clinic.timezone).time() != self.requested_time
                for slot in self.options[1:]
            ):
                return self._list_options(
                    f"{prefix}Here are the {requested} openings and the closest alternatives."
                )
        return self._list_options(f"{prefix}Which time works for you?")

    def _selection(self, words: str) -> Resource | None:
        match = re.fullmatch(
            r"(?:please )?(?:(?:i'll take|i will take|let's do|i'd like|i would like|"
            r"(?:(?:can|could|would) you (?:please )?)?(?:book|schedule)) )?"
            r"(?:the )?(?:option |number )?(one|two|three|1|2|3|first|second|third)"
            r"(?: one| option)?(?: (?:is fine|works(?: for me)?|sounds good))?(?: please)?",
            words,
        )
        if match is None:
            return None
        index = {
            "one": 0,
            "1": 0,
            "first": 0,
            "two": 1,
            "2": 1,
            "second": 1,
            "three": 2,
            "3": 2,
            "third": 2,
        }[match[1]]
        return self.options[index] if len(self.options) > index else None

    def _offered_time(self, text: str, words: str) -> Resource | None:
        requested_time = parse_time(words)
        if requested_time is None or re.search(
            r"\b(?:no|not|can't|cannot|don't|won't|after|before|around|between|instead|tomorrow|next week|another|other|any|available)\b",
            words,
        ):
            return None
        clinic = self.scheduling.clinic
        if (
            self._provider(words)
            or re.search(r"\b(?:doctor|dr\.?|location)\b", words)
            or any(
                re.search(rf"\b{re.escape(location)}\b", words)
                for location in (
                    *clinic.locations,
                    *(name.casefold() for name in clinic.locations.values()),
                )
            )
        ):
            return None
        zone = clinic.timezone
        today = self.scheduling.now().astimezone(zone).date()
        days = requested_days(words, today)
        if len(days) > 1:
            return None
        matches = [
            slot
            for slot in self.options
            if (start := instant(slot["start"]).astimezone(zone)).time() == requested_time
            and (not days or start.date() in days)
        ]
        # A bare time or an acceptance selects the matching offer. Availability
        # questions should still go through the search, even if the time matches.
        if text.rstrip().endswith("?") and not re.search(r"\b(?:book|take|schedule)\b", words):
            return None
        if len(matches) == 1 and (
            days
            or not re.search(
                r"\b(?:today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                words,
            )
        ):
            return matches[0]
        return None

    async def _select_existing(self, words: str) -> Reply:
        self.old = self._selection(words)
        if self.old is None:
            return self.say("Please say the option number.")
        if not self.scheduling.can_change(self.old):
            return self.say(
                "This appointment is too close to change here. Please contact staff.",
                outcome="handoff",
            )
        if self.intent == "reschedule":
            duration = instant(self.old["end"]) - instant(self.old["start"])
            self.visit = "first visit" if duration == timedelta(minutes=40) else "follow-up"
            return await self._offer()
        self.change = Change(uuid4().hex, "cancel", self._patient(), self.old, None)
        self.stage = "confirm_change"
        return self._read_change()

    def _read_change(self, prefix: str = "") -> Reply:
        change = self.change
        assert change is not None
        if change.action == "cancel":
            assert change.old is not None
            text = f"Cancel your appointment on {self.describe(change.old)}?"
        else:
            assert change.hold is not None
            time = self.describe(change.hold.slot)
            if change.action == "reschedule":
                assert change.old is not None
                text = f"Move your appointment from {self.describe(change.old)} to {time}?"
            else:
                text = f"Book your {self.visit} on {time}?"
                if change.new_patient is not None:
                    text = f"Create a new demo patient record and book your first visit on {time}?"
        return self.say(
            prefix + text + " Please say yes or no.",
            confirm=True,
            calendar=self._calendar([change.hold.slot], "held") if change.hold else (),
        )

    async def _prepare_confirmation(self) -> Reply:
        if self.frontdesk and not self.email_checked:
            stored = (
                await self.scheduling.email_on_file(self._patient())
                if not self.new_patient
                else None
            )
            self.email_on_file = email_address(stored) if stored else None
            self.email_checked = True
            if not self.email_on_file:
                self.stage = "email_address"
                return self.say(
                    "What email address should I use for your appointment confirmation?",
                    calendar=self._held_calendar(),
                )
        self.stage = "confirm_change"
        prefix = "I'll use your email on file for the confirmation. " if self.email_on_file else ""
        return self._read_change(prefix)

    def _held_calendar(self) -> tuple[dict, ...]:
        if self.change and self.change.hold:
            return self._calendar([self.change.hold.slot], "held")
        return ()

    def _capture_email(self, text: str, words: str) -> Reply:
        if words in {"skip email", "skip the email", "no email"}:
            self.email_on_file = self.pending_email = None
            self.stage = "confirm_change"
            return self._read_change("Of course. ")
        spoken = re.sub(r"^(?:my email (?:address )?is|it['’]s)\s+", "", text.strip(), flags=re.I)
        for word, punctuation in (
            ("at", "@"),
            ("dot", "."),
            ("dash", "-"),
            ("underscore", "_"),
            ("plus", "+"),
        ):
            spoken = re.sub(rf"\s+{word}\s+", punctuation, spoken, flags=re.I)
        candidate = email_address("".join(spoken.rstrip(".,!?").split()))
        if candidate:
            self.pending_email = candidate
        if self.pending_email is None:
            return self.say(
                "Could you repeat the email address slowly? If you don't have one, say skip email.",
                calendar=self._held_calendar(),
            )
        self.stage = "email_readback"
        readback = self.pending_email.replace("@", " at ").replace(".", " dot ")
        return self.say(
            f"I heard {readback}. Is that the right email address?",
            confirm=True,
            calendar=self._held_calendar(),
        )

    async def _confirm(self, words: str) -> Reply:
        if (
            self.frontdesk
            and self.intent != "cancel"
            and re.fullmatch(
                r"(?:please )?(?:change|update|use a different) (?:my |the )?email(?: address)?",
                words,
            )
        ):
            self.stage = "email_address"
            return self.say("What email address should I use for this appointment confirmation?")
        answer = answer_to_confirmation(words, self.intent)
        if answer == "no":
            await self.close()
            return self.say("Okay, I made no changes.", outcome="declined")
        confirmed = answer == "yes"
        if self.intent != "cancel" and not confirmed and await self._update_preferences(words):
            await self.close()
            return await self._offer()
        if self.intent != "cancel" and MORE_TIMES.search(words):
            await self.close()
            return await self._offer(more=True)
        if not confirmed or not self.played:
            if self.frontdesk and (answer := self._scheduling_question(words)):
                return self._read_change(answer + " ")
            if self.frontdesk and not confirmed:
                return self._read_change("I didn't catch whether you'd like me to go ahead. ")
            return self._read_change()
        change = self.change
        assert change is not None
        if self.service and not await self.scheduling.has_referral(change.patient, self.service):
            await self.close()
            return self._referral_handoff()
        self.interrupt()
        self.last_operation = change.operation
        if change.new_patient and self.email_on_file:
            patient = {
                **change.new_patient,
                "telecom": [{"system": "email", "value": self.email_on_file}],
            }
            change = replace(change, new_patient=patient)
        if self.frontdesk:
            try:
                details = ""
                if change.hold:
                    start = instant(change.hold.slot["start"]).astimezone(
                        self.scheduling.clinic.timezone
                    )
                    details = f"{self.describe(change.hold.slot)}\nYear: {start.year}\nClinic time zone: {self.scheduling.clinic.timezone.key}"
                await self.frontdesk.stage(
                    change,
                    self.trace.id if self.trace else None,
                    self.scheduling.clinic.name,
                    details,
                    self.email_on_file,
                )
            except sqlite3.Error as exc:
                raise Unavailable("Front desk journal unavailable") from exc
        if self.trace:
            try:
                await self.trace.begin_mutation(change.operation, change.action)
            except sqlite3.Error as exc:
                raise Unavailable("Operation journal unavailable") from exc
        status = "unknown"
        try:
            await self.scheduling.commit(change)
            status = "committed"
        except Conflict:
            status = "rejected"
            raise
        finally:
            if self.trace:
                try:
                    await self.trace.finish_mutation(change.operation, status)
                except sqlite3.Error:
                    LOG.warning("Operation journal update unavailable")
        self.change = None
        outcome = {"book": "booked", "reschedule": "rescheduled", "cancel": "cancelled"}[
            change.action
        ]
        if change.hold:
            delivery = None
            email_note = ""
            if self.frontdesk:
                try:
                    delivery = await self.frontdesk.activate_notice(change.operation)
                except sqlite3.Error:
                    delivery = "awaiting_commit"
                if delivery in {"queued", "sent", "sending"}:
                    email_note = " Your confirmation email is queued."
                elif delivery == "captured":
                    email_note = " Your confirmation was saved to the demo outbox."
                elif delivery == "needs_address":
                    email_note = " The front desk will need an email address for the confirmation."
                else:
                    email_note = " The front desk can check your confirmation email status."
            return self.say(
                f"You're {outcome} for {self.describe(change.hold.slot)}." + email_note,
                outcome=outcome,
                calendar=self._calendar([change.hold.slot], "booked"),
                email_status=delivery if self.email_form else None,
            )
        return self.say("Your appointment was cancelled.", outcome=outcome)


DAY_ORDINALS = {
    word: day
    for day, word in enumerate(
        (
            "first",
            "second",
            "third",
            "fourth",
            "fifth",
            "sixth",
            "seventh",
            "eighth",
            "ninth",
            "tenth",
            "eleventh",
            "twelfth",
            "thirteenth",
            "fourteenth",
            "fifteenth",
            "sixteenth",
            "seventeenth",
            "eighteenth",
            "nineteenth",
            "twentieth",
            "twenty first",
            "twenty second",
            "twenty third",
            "twenty fourth",
            "twenty fifth",
            "twenty sixth",
            "twenty seventh",
            "twenty eighth",
            "twenty ninth",
            "thirtieth",
            "thirty first",
        ),
        start=1,
    )
}
DAY_ORDINAL_PATTERN = "|".join(
    word.replace(" ", r"[-\s]+") for word in sorted(DAY_ORDINALS, key=len, reverse=True)
)

YEAR_ONES = {
    word: number
    for number, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen".split()
    )
}
YEAR_TENS = dict(
    zip(
        "twenty thirty forty fifty sixty seventy eighty ninety".split(),
        range(20, 100, 10),
        strict=True,
    )
)
YEAR_TAIL = (
    rf"(?:oh (?:{'|'.join(list(YEAR_ONES)[1:10])})|{'|'.join(YEAR_ONES)}|"
    rf"(?:{'|'.join(YEAR_TENS)})(?:[ -](?:{'|'.join(list(YEAR_ONES)[1:10])}))?)"
)
SPOKEN_YEAR = re.compile(
    rf"\b(?:(?P<thousand>two thousand)(?: (?:and )?(?P<remainder>{YEAR_TAIL}))?"
    rf"|(?P<century>nineteen|twenty) (?P<tail>{YEAR_TAIL}|hundred))\b"
)


def _spoken_year(match: re.Match[str]) -> str:
    base = 2000 if match["thousand"] else 100 * YEAR_ONES.get(match["century"], 20)
    tail = match["remainder"] if match["thousand"] else match["tail"]
    if tail is None or tail == "hundred":
        return str(base)
    return str(
        base
        + sum(
            YEAR_ONES.get(word, YEAR_TENS.get(word, 0)) for word in tail.replace("-", " ").split()
        )
    )


def _date_words(text: str) -> str:
    text = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", text.casefold())
    text = SPOKEN_YEAR.sub(_spoken_year, " ".join(re.sub(r"[.,]", " ", text).split()))
    text = re.sub(
        rf"\b(?:{DAY_ORDINAL_PATTERN})\b",
        lambda match: str(DAY_ORDINALS[" ".join(match[0].replace("-", " ").split())]),
        text,
    )
    return " ".join(re.sub(r"[.,]", " ", text).split())


def date_candidates(text: str, *, reference: date | None = None) -> tuple[date, ...]:
    words = _date_words(text)
    month = rf"(?P<month>{MONTH_PATTERN})"
    year, day = r"(?P<year>\d{4})", r"(?P<day>\d{1,2})"
    patterns = [
        rf"{day}\s+(?:of\s+)?{month}\s+{year}",
        rf"{month}\s+(?:the\s+)?{day}\s+{year}",
        rf"{year}\s+{month}\s+{day}",
        rf"{year}\s+{day}\s+(?:of\s+)?{month}",
        rf"{year}[-/ ](?P<month>\d{{1,2}})[-/ ]{day}",
        rf"{year}\s+(?P<month>\d{{2}})(?P<day>\d{{2}})",
        rf"(?P<month>\d{{1,2}})/{day}/{year}",
    ]
    if reference:
        patterns.extend([rf"{month}\s+(?:the\s+)?{day}", rf"{day}\s+(?:of\s+)?{month}"])
    found = {}
    spans = []
    for pattern in patterns:
        for match in re.finditer(rf"\b{pattern}\b", words):
            start, end = match.span()
            if any(start < right and end > left for left, right in spans):
                continue
            spans.append((start, end))
            raw_month = match["month"]
            month_number = int(raw_month) if raw_month.isdecimal() else MONTHS[raw_month]
            day_number = int(match["day"])
            year_number = match.groupdict().get("year")
            if year_number is None:
                assert reference is not None
                year_number = reference.year + (
                    (month_number, day_number) < (reference.month, reference.day)
                )
            try:
                found[start] = date(int(year_number), month_number, day_number)
            except ValueError:
                continue
    return tuple(dict.fromkeys(found[key] for key in sorted(found)))


def parse_date(text: str, *, reference: date | None = None) -> date | None:
    dates = date_candidates(text, reference=reference)
    return dates[0] if len(dates) == 1 else None


def requested_days(text: str, today: date) -> tuple[date, ...]:
    words = _date_words(text)
    month = rf"(?P<month>{MONTH_PATTERN})"
    for pattern in (
        rf"(?P<first>\d{{1,2}}) (?:or|and) (?P<second>\d{{1,2}}) (?:of )?{month}",
        rf"{month} (?P<first>\d{{1,2}}) (?:or|and) (?P<second>\d{{1,2}})",
    ):
        match = re.search(rf"\b{pattern}(?: (?P<year>\d{{4}}))?\b", words)
        if match:
            suffix = f" {match['year']}" if match["year"] else ""
            return tuple(
                dict.fromkeys(
                    day
                    for key in ("first", "second")
                    if (
                        day := parse_date(f"{match['month']} {match[key]}{suffix}", reference=today)
                    )
                )
            )
    return date_candidates(words, reference=today)


def parse_time(text: str, *, clinic_hours: bool = False) -> time | None:
    match = re.search(r"\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?\s*m\b", text, re.I)
    if match:
        hour = int(match[1])
        if 1 <= hour <= 12:
            return time(hour % 12 + (12 if match[3].lower() == "p" else 0), int(match[2] or 0))
        return None
    match = re.search(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if match:
        return time(int(match[1]), int(match[2]))
    if re.search(r"\b(?:noon|midday)\b", text):
        return time(12)
    if clinic_hours:
        spoken = dict(
            zip(
                (
                    "one",
                    "two",
                    "three",
                    "four",
                    "five",
                    "six",
                    "seven",
                    "eight",
                    "nine",
                    "ten",
                    "eleven",
                    "twelve",
                ),
                range(1, 13),
                strict=True,
            )
        )
        token = text.split(maxsplit=1)[0]
        hour = int(token) if token.isdecimal() and len(token) <= 2 else spoken.get(token)
        if hour is not None and 1 <= hour <= 12:
            return time(hour + 12 if hour <= 7 else hour)
    return None
