from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from switchboard.frontdesk import email_address
from switchboard.intents import TurnInterpreter, TurnMeaning
from switchboard.safety import EMERGENCY_SCRIPT, SafetySignal, condition_only, safety_signal
from switchboard.scheduling import Change, Scheduling, instant
from switchboard.spoken import (
    MONTH_PATTERN,
    birth_dates,
    date_words,
    parse_date,
    parse_time,
    requested_days,
    spoken_time,
)
from switchboard.store import Conflict, DemoStore, Resource, Unavailable
from switchboard.understanding import (
    answer_to_confirmation,
    booking_recipient,
    introduces_name,
    is_booking_request,
    leading_answer,
    model_says_self,
    name_from_reply,
    opening_name,
    proxy_request,
    qualified,
    request_route,
    states_name,
)

if TYPE_CHECKING:
    from switchboard.frontdesk import FrontDesk
    from switchboard.operations import Trace

LOG = logging.getLogger(__name__)

EXPLICIT_STAFF = re.compile(
    r"\b(?:speak|talk|connect|transfer|put me through)\b.{0,45}"
    r"\b(?:human|person|someone|staff|nurse|representative|front desk)\b"
    r"|^(?:(?:i (?:need|want)|i'd like) (?:a |the )?)?"
    r"(?:human|staff|representative|front desk)(?: please)?[.!]?$",
    re.I,
)
STAFF_TOPIC = re.compile(
    r"\b(?:refills?|test results|medical advice|billing|dosage|should i take|is it normal"
    r"|running (?:a little |a bit )?late|(?:be|am|was) (?:a little |a bit )?late|late for"
    r"|update (?:my |the )?(?:insurance|coverage)"
    r"|(?:change|replace|remove|add) (?:the |my |an? )?(?:insurance|coverage)(?: on file)?"
    r"|(?:new|different) insurance (?:card|on file))\b",
    re.I,
)
PROVIDER_TITLE = re.compile(
    r"\b(?:(?:the|a|any|my|our|your)\s+doctor\b|(?:dr\.?|doctor)\s+([^\W\d_]+))", re.I
)
PROVIDER_NON_NAMES = frozenset(
    "today tomorrow tonight this next now later soon again on at in around before after "
    "monday tuesday wednesday thursday friday saturday sunday please is can available appointment".split()
)
MORE_TIMES = re.compile(
    r"\b(?:(?:other|more|different|additional) (?:times|openings|slots|options)|what else (?:is available|do you have))\b"
)
TURN_ACTIONS = {
    "intent": {
        "book": "Request a new appointment for anyone. Value may contain explicit scheduling preferences only",
        "reschedule": "Move an existing appointment. Value may contain explicit scheduling preferences only",
        "cancel": "Cancel an existing appointment",
        "insurance": "Ask what insurance the clinic accepts",
        "coverage": "Check the caller's own coverage",
    },
    "booking_recipient": {
        "self": "Caller explicitly and unambiguously identifies themself as the patient. "
        "Value must quote their exact latest reply. A name, yes, or an ambiguous reply is unclear",
        "other": "Caller identifies another person as the patient. Value is empty",
    },
    "select_existing": {"more": "Caller asks to hear more existing appointments"},
    "name": {"name": "Extract only the name explicitly given in the latest reply"},
    "dob": {
        "birth_date": "Normalize the stated birth date to YYYY-MM-DD, Month DD, or YYYY alone",
        "keypad": "Caller asks to enter the date on the phone keypad",
    },
    "correct_identity": {
        "name": "Extract only the replacement name explicitly stated",
        "birth_date": "Normalize the complete replacement birth date to YYYY-MM-DD",
        "change_name": "Caller asks to correct their name but does not give the replacement",
        "change_date": "Caller asks to correct their birth date but does not give the replacement",
    },
    "email_address": {
        "email": "Normalize the email address actually stated, including spoken at and dot",
        "skip_email": "Caller declines to provide email",
    },
    "other_provider": {
        "preferences": "Value is the explicitly requested provider or scheduling preference",
    },
    "day_alternative": {
        "preferences": "Normalize a requested date or time without adding missing details",
        "more": "Caller asks to see more openings",
    },
    "confirm_change": {
        "preferences": "Caller requests different scheduling details. Normalize the stated details",
        "more": "Caller asks to see more openings",
        "change_email": "Caller asks to use a different email address",
        "email_question": "Caller asks if or how they will get an email confirmation",
    },
    "select_slot": {
        "preferences": "Caller requests a different time, date, provider or location. Value is the normalized request.",
        "more": "Caller explicitly asks for additional or different openings",
        "repeat": "Caller asks to hear the current offers again, including asking what their choices were",
        "waitlist": "Caller asks to be added to the waitlist",
    },
}
CONFIRMATION_STAGES = {
    "check_name",
    "check_dob",
    "email_readback",
    "confirm_change",
    "other_provider",
    "day_alternative",
    "proxy_booking",
}


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
        router: TurnInterpreter | None = None,
        trace: Trace | None = None,
        *,
        allow_registration: bool = False,
        frontdesk: FrontDesk | None = None,
        email_form: bool = False,
        voice: bool = False,
    ):
        self.trace = trace
        self.voice = voice
        self.scheduling = scheduling
        self.interpreter = router
        self.allow_registration = allow_registration
        self.proxy_booking_allowed = bool(frontdesk and isinstance(scheduling.store, DemoStore))
        self.booking_for_other = False
        self.frontdesk = frontdesk
        self.email_form = email_form
        self.stage = "intent"
        self.safety_resume: Reply | None = None
        self.safety_opening: str | None = None
        self.safety_retries = 0
        self.pending_booking: tuple[str, str, TurnMeaning | None] | None = None
        self.patient: str | None = None
        self.name = ""
        self.dob: date | None = None
        self.dob_fragment: str | None = None
        self.dob_retries = 0
        self.identity_playback_retries = 0
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
        self.opening_interpreter_failures = 0
        self.digits = ""
        self.provider: str | None = None
        self.options: list[Resource] = []
        self.existing_options: list[Resource] = []
        self.shown_slots: set[str] = set()
        self.old: Resource | None = None
        self.change: Change | None = None
        self.prompt_id: str | None = None
        self.played = False
        self.history: deque[dict[str, str]] = deque(maxlen=6)
        self.last_reply: Reply | None = None

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
        self.last_reply = reply
        self.history.append({"assistant": text[:400]})
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
            if (
                self.staff_topic
                and self.staff_topic != "proxy booking"
                and outcome in {"booked", "declined", "abandoned"}
            ):
                self._work("staff_requested")
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
        clock = spoken_time(start.time()) if self.voice else start.strftime("%I:%M %p")
        return f"{start:%A, %B} {start.day} at {clock} with {name}" + (
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
        previous = self.last_reply
        if self.trace:
            self.trace.record("caller", text)
        started = monotonic()
        try:
            return await self._handle_turn(text)
        finally:
            # The reply is recorded by say(). Keep the transcript in turn order.
            if self.last_reply is not previous and self.history and "assistant" in self.history[-1]:
                last = self.history.pop()
                self.history.extend([{"caller": text[:400]}, last])
            if self.trace:
                self.trace.add(
                    "turn_processed",
                    state=f"{stage} → {self.stage}",
                    latency_ms=round((monotonic() - started) * 1000),
                )

    async def _handle_turn(self, text: str, *, safety_clarification: str | None = None) -> Reply:
        text = text.strip()
        words = " ".join(text.lower().replace(",", " ").replace("’", "'").strip(".!? ").split())
        signal = safety_signal(text)
        if signal is SafetySignal.ACUTE:
            return self.emergency_reply()
        if self.stage == "closed":
            return self.say("This call has ended.", outcome="closed")
        try:
            try:
                meaning = await self._interpret_turn(
                    text
                    if safety_clarification is None
                    else f"{text}\nWhen asked if the condition is happening now, the caller answered: {safety_clarification}"
                )
                if meaning and meaning.emergency:
                    return self.emergency_reply()
                if self.safety_resume:
                    lead = leading_answer(text)
                    if lead.answer == "yes" or (meaning and meaning.action == "current_danger"):
                        return self.emergency_reply()
                    if (
                        lead.answer == "no"
                        or answer_to_confirmation(words) == "no"
                        or (meaning and meaning.action == "historical")
                    ):
                        opening, self.safety_opening = self.safety_opening, None
                        previous, self.safety_resume = self.safety_resume, None
                        self.safety_retries = 0
                        # "No, it was in the past, I need to book a follow-up" carries a request.
                        if (
                            self.stage == "intent"
                            and lead.answer == "no"
                            and lead.rest
                            and request_route(lead.rest) != "unknown"
                        ):
                            return await self._handle_turn(lead.rest, safety_clarification=text)
                        if opening is not None and not condition_only(opening):
                            return await self._handle_turn(opening, safety_clarification=text)
                        return self.say(
                            previous.text,
                            confirm=previous.confirmation,
                            calendar=previous.calendar,
                            email_status=previous.email_status,
                        )
                    self.safety_retries += 1
                    if self.safety_retries >= 2:
                        await self.close()
                        return self._handoff(
                            "staff_requested",
                            "I couldn't tell whether this is happening now. "
                            "If this is an emergency, call 911 now. The front desk can help with scheduling.",
                        )
                if self.safety_resume or (
                    safety_clarification is None
                    and signal is SafetySignal.AMBIGUOUS
                    and (meaning is None or meaning.action == "unclear" or condition_only(text))
                ):
                    if not self.safety_resume:
                        self.safety_resume = self.last_reply or self.greeting()
                        if self.stage == "intent":
                            self.safety_opening = text
                    return self.say(
                        "Is this happening right now? If it is an emergency, call 911 now."
                    )
                staff = STAFF_TOPIC.search(words)
                proxy = proxy_request(words)
                if proxy and proxy.form == "possessive":
                    # "my mom's appointment" keeps today's handling until proxy policy covers it.
                    proxy = None
                book_request = is_booking_request(words)
                booking = self.stage == "intent" and (
                    book_request or (not staff and bool(meaning and meaning.action == "book"))
                )
                if booking and self.frontdesk and not EXPLICIT_STAFF.search(words):
                    if proxy:
                        if self.proxy_booking_allowed:
                            self.pending_booking = (words, text, meaning)
                        else:
                            self._set_window(words)
                        return self._ask_proxy_permission()
                if (
                    self.stage in {"booking_recipient", "proxy_booking"}
                    and words != "0"
                    and not EXPLICIT_STAFF.search(words)
                ):
                    return await self._handle(text, words, meaning)
                grounded_reply = (
                    (self.stage == "dob" and bool(birth_dates(text, self.scheduling.now().date())))
                    or (self.stage == "name" and bool(name_from_reply(text)))
                    or (
                        self.stage in CONFIRMATION_STAGES
                        and answer_to_confirmation(words) is not None
                    )
                    or (
                        self.stage in {"select_slot", "select_existing"}
                        and self._selection(words) is not None
                    )
                )
                if (
                    words == "0"
                    or EXPLICIT_STAFF.search(words)
                    or proxy
                    or (
                        (
                            staff
                            or (
                                meaning
                                and (meaning.action == "staff" or meaning.needs_staff)
                                and not grounded_reply
                            )
                        )
                        and not booking
                    )
                ):
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
                if booking and (staff or (meaning and meaning.needs_staff)):
                    self.staff_topic = staff[0] if staff else "additional caller request"
                if (
                    meaning
                    and meaning.action in {"hours", "locations", "accepted_insurance"}
                    and not booking
                    and not grounded_reply
                ):
                    response = self._clinic_answer(meaning.action)
                    if self.stage == "intent":
                        return self.say(response, outcome="info_only")
                    if previous := self.last_reply:
                        return self.say(
                            f"{response} {previous.text}",
                            confirm=previous.confirmation,
                            calendar=previous.calendar,
                        )
                return await self._handle(text, words, meaning)
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

    def emergency_reply(self, text: str | None = None) -> Reply:
        if text and self.trace:
            self.trace.record("caller", text)
        reply = self.say(EMERGENCY_SCRIPT, outcome="emergency")
        if self.trace:
            self.trace.add("emergency_routed")
        return reply

    async def _interpret_turn(self, text: str) -> TurnMeaning | None:
        if not self.interpreter:
            return None
        actions = {
            **TURN_ACTIONS.get(self.stage, {}),
            "staff": "The caller requests staff or asks a clinical question",
            "hours": "The caller asks when the clinic is open",
            "locations": "The caller asks where the clinic is located",
            "accepted_insurance": "The caller asks which insurers the clinic accepts",
        }
        if self.safety_resume:
            actions["current_danger"] = (
                "Caller explicitly confirms the possible emergency is happening now."
            )
            actions["historical"] = (
                "Caller explicitly says the condition is historical, resolved, or hypothetical."
            )
        if self.stage in CONFIRMATION_STAGES:
            actions.update(
                affirm="Clearly and unconditionally agrees to the exact last question. "
                "For booking changes, bare acknowledgments like okay or right are insufficient.",
                decline="Clearly says no to the last question",
            )
        if self.stage in {"check_name", "check_dob", "email_readback"}:
            actions.update(
                TURN_ACTIONS["correct_identity"]
                if self.stage != "email_readback"
                else TURN_ACTIONS["email_address"]
            )
        context = {
            "stage": self.stage,
            "request": self.intent,
            "recent": list(self.history)[-3:],
            "caller": text,
        }
        clinic = self.scheduling.clinic
        context["clinic"] = {
            "providers": list(clinic.providers.values()),
            "locations": list(clinic.locations.values()),
            "services": clinic.referral_services,
            "today": str(self.scheduling.now().astimezone(clinic.timezone).date()),
        }
        if self.stage in {"select_slot", "select_existing"}:
            # The options carry the current offer. Avoid repeating it in the history.
            context.pop("recent", None)
            if self.last_reply:
                context["prompt"] = self.last_reply.text.split("Option 1:", 1)[0].strip()[:200]
            context["options"] = dict(enumerate(self._offer_descriptions(), 1))
            if self.options:
                actions["select"] = (
                    "Caller clearly chooses one of the listed offers. Value is its option number. "
                    "Use chronological labels for time references and the numbers for offer order. "
                    "Refusals, questions, and multiple possible choices are not selections."
                )
        try:
            async with asyncio.timeout(8):
                result = await self.interpreter.interpret_turn(context, actions)
        except TimeoutError:
            result = None
        if self.trace:
            self.trace.add(
                "context_interpreted" if result else "context_unresolved", state=self.stage
            )
        return result

    def _context_answer(
        self, words: str, meaning: TurnMeaning | None, *, change: bool = False
    ) -> str | None:
        exact = answer_to_confirmation(words, self.intent if change else None)
        if change or not meaning or exact:
            return exact
        if meaning.action == "decline":
            return "no"
        # Reject qualifications and contradictory evidence before interpreting consent.
        if meaning.action == "affirm" and not qualified(words):
            return "yes"
        return None

    def _ask_proxy_permission(self) -> Reply:
        self.stage = "proxy_booking"
        if self.proxy_booking_allowed:
            return self.say("Do you have permission to book a new visit for this patient?")
        self.staff_topic = "proxy booking"
        return self.say(
            "The front desk can help book visits for someone else. "
            "Would you like me to send them your scheduling request?"
        )

    async def _handle(self, text: str, words: str, meaning: TurnMeaning | None = None) -> Reply:
        answer = self._context_answer(words, meaning)
        action = meaning.action if meaning else ""
        value = meaning.value.strip() if meaning else ""
        match self.stage:
            case "intent":
                return await self._start(words, text, meaning)
            case "booking_recipient":
                recipient = booking_recipient(text)
                if action == "other" or recipient.kind == "other":
                    if not self.proxy_booking_allowed:
                        self.pending_booking = None
                    return self._ask_proxy_permission()
                if recipient.kind == "self" or (action == "self" and model_says_self(text, value)):
                    assert self.pending_booking is not None
                    opening_words, opening_text, opening_meaning = self.pending_booking
                    self.pending_booking = None
                    return await self._start(
                        opening_words, opening_text, opening_meaning, recipient_confirmed=True
                    )
                self.unclear += 1
                if self.unclear >= 3:
                    self.pending_booking = None
                    return self._handoff(
                        "staff_requested",
                        "I couldn't confirm who the appointment is for. The front desk can help.",
                    )
                return self.say("Is the appointment for you or for someone else?")
            case "proxy_booking":
                if self.proxy_booking_allowed:
                    if answer_to_confirmation(words) == "no":
                        self.pending_booking = None
                        return self.say("Okay. I haven't booked anything.", outcome="declined")
                    if answer_to_confirmation(words, "book") == "yes":
                        assert self.pending_booking is not None
                        opening_words, opening_text, opening_meaning = self.pending_booking
                        self.pending_booking = None
                        self.booking_for_other = True
                        self.name, self.dob = "", None
                        return await self._start(
                            opening_words, opening_text, opening_meaning, recipient_confirmed=True
                        )
                    return self.say(
                        "Do you have permission to book for the patient? Please say yes or no."
                    )
                if answer == "yes":
                    return self._handoff(
                        "staff_requested", "I'll pass your booking request to the front desk."
                    )
                if answer == "no":
                    return self.say("Okay. I haven't sent a request.", outcome="declined")
                if await self._update_preferences(words):
                    return self.say(
                        "I have your scheduling preferences. Should I send this request to the front desk?"
                    )
                return self.say("Should I send the front desk your booking request?")
            case "other_provider":
                self.provider = self._provider(words) or (
                    self._provider(value.casefold()) if action == "preferences" else None
                )
                if self.provider or answer == "yes" or words in {"anyone", "another doctor"}:
                    self._set_window(words)
                    return self._ask_name()
                if answer == "no":
                    return self.say("Of course. Thanks for calling.", outcome="declined")
                return self.say("Would you like to hear times with another doctor here?")
            case "name":
                if (
                    action == "name"
                    and value
                    and value.casefold() in text.casefold()
                    and not self._identity_details(text)
                ):
                    return self._capture_name(value)
                return self._capture_name(text)
            case "check_name":
                if answer == "yes" and self.played:
                    self.stage = "dob"
                    return self.say(
                        "What's the patient's date of birth?"
                        if self.booking_for_other
                        else "Thanks. What's your date of birth?"
                    )
                if answer == "no" or words in {"change it", "change my name", "the name"}:
                    self.name = ""
                    return self._ask_name()
                if states_name(text):
                    return self._capture_name(text, confirm=True)
                if action in TURN_ACTIONS["correct_identity"]:
                    return self._apply_identity_correction(text, words, action, value)
                return self._ask_name()
            case "day_alternative":
                if answer == "yes" or MORE_TIMES.search(words):
                    self.day = None
                    self.days = ()
                    return await self._offer()
                if answer == "no":
                    return self.say("Of course. I haven't booked anything.", outcome="declined")
                if await self._update_preferences(words) or (
                    action == "preferences" and await self._update_preferences(value.casefold())
                ):
                    return await self._offer()
                return self.say(
                    "Would you like the next available day, or is there another day you prefer?"
                )
            case "dob":
                if words in {"keypad", "use the keypad"} or (
                    action == "keypad" and re.search(r"\b(?:keypad|keys|type)\b", words)
                ):
                    self.stage = "keypad"
                    self.digits = ""
                    return self.say("Enter eight digits in year, month, day order.")
                dates = birth_dates(text, self.scheduling.now().date())
                if len(dates) > 1:
                    self.dob_fragment = None
                    return self.say(
                        "I heard two different birth dates. Could you say your birth date once?"
                    )
                if not dates and action == "birth_date" and value:
                    text = value
                    dates = birth_dates(text, self.scheduling.now().date())
                self.dob = next(iter(dates), None)
                if self.dob is None and self.dob_fragment:
                    years = set(re.findall(r"\b\d{4}\b", date_words(text)))
                    if len(years) == 1:
                        self.dob = parse_date(f"{self.dob_fragment} {years.pop()}")
                if self.dob is None:
                    fragment = re.search(
                        rf"\b(?:\d{{1,2}}\s+(?:(?:of|to)\s+)?(?:{MONTH_PATTERN})|"
                        rf"(?:{MONTH_PATTERN})\s+\d{{1,2}})\b",
                        date_words(text),
                    )
                    if fragment and not re.search(r"\b\d{4}\b", text):
                        self.dob_fragment = fragment[0].replace(" to ", " of ")
                        return self.say("I caught the month and day. What year were you born?")
                if self.dob is None or self.dob > self.scheduling.now().date():
                    self.dob = None
                    self.dob_retries += 1
                    if self.voice and self.dob_retries >= 3:
                        return self._handoff(
                            "identity_unverified",
                            "I couldn't confirm the birth date. The front desk can help.",
                        )
                    if self.dob_retries >= 2:
                        return self.say(
                            "Say the month first, then the day and four-digit year. "
                            "You can also say use the keypad or ask for the front desk."
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
                    if action in TURN_ACTIONS["correct_identity"]:
                        return self._apply_identity_correction(text, words, action, value)
                    return self._correct_identity(text, words)
                return await self._verify()
            case "correct_identity":
                return self._apply_identity_correction(text, words, action, value)
            case "select_existing":
                return await self._select_existing(text, words, meaning)
            case "select_slot":
                if MORE_TIMES.search(words):
                    return await self._offer(more=True)
                if re.search(
                    r"\b(?:closer to|near|around)\b", words
                ) and await self._update_preferences(words):
                    return await self._offer()
                if re.fullmatch(r"(?:option )?[1-3]", words):
                    option = self._selection(words)
                else:
                    option = self._choose_option(text, words, meaning=meaning)
                if self.frontdesk and action == "waitlist":
                    return await self._join_waitlist()
                if action == "more":
                    return await self._offer(more=True)
                if action == "repeat":
                    return self._list_options("Here are the options again.")
                if action == "preferences" and await self._update_preferences(value.casefold()):
                    return await self._offer()
                if option is None:
                    if self.frontdesk and re.search(
                        r"\b(?:join|add me to|put me on) (?:the )?wait ?list\b", words
                    ):
                        return await self._join_waitlist()
                    if self.frontdesk and (answer := self._scheduling_question(words)):
                        return self._list_options(answer)
                    if await self._update_preferences(words):
                        return await self._offer()
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
                return await self._confirm(words, meaning)
            case "email_address":
                if action == "skip_email":
                    return self._capture_email(text, "skip email")
                if action == "email" and email_address(value):
                    return self._capture_email(value, words)
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
                if action == "skip_email":
                    return self._capture_email(text, "skip email")
                return self._capture_email(
                    value if action == "email" and email_address(value) else text, words
                )
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

    async def _start(
        self,
        words: str,
        text: str,
        meaning: TurnMeaning | None = None,
        *,
        recipient_confirmed: bool = False,
    ) -> Reply:
        if not recipient_confirmed:
            if (
                self.frontdesk
                and re.search(r"\b(locations?|where are you|addresses)\b", words)
                and not re.search(r"\b(book|schedule|appointment)\b", words)
            ):
                return self.say(self._locations(), outcome="info_only")
            if re.search(r"\b(hours|when are you open)\b", words):
                return self.say(
                    f"The clinic is open {self.scheduling.clinic.weekday_hours}.",
                    outcome="info_only",
                )
            if (meaning and meaning.action == "coverage") or (
                not is_booking_request(words)
                and re.search(r"\bmy (insurance|coverage|plan)\b", words)
                and not re.search(r"\b(accept|take)\b", words)
            ):
                self.intent = "coverage"
                return self._ask_name()
        intent = (
            "book"
            if recipient_confirmed
            else meaning.action
            if meaning and meaning.action in {"book", "reschedule", "cancel", "insurance"}
            else "book"
            if self.staff_topic and is_booking_request(words)
            else request_route(words)
        )
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
            if self.interpreter and meaning is None:
                self.opening_interpreter_failures += 1
                if self.opening_interpreter_failures >= 2:
                    return self._handoff(
                        "staff_requested",
                        "I'm still having trouble understanding the request. The front desk can help.",
                    )
                return self.say(
                    "I had trouble understanding the request. Could you repeat it? "
                    "You can also ask for the front desk."
                )
            self.opening_interpreter_failures = 0
            if self.frontdesk and not self.booking_for_other and (name := opening_name(text)):
                self.name = name
            self.unclear += 1
            if self.frontdesk and self.unclear >= 3:
                return self._handoff(
                    "staff_requested", "I'll ask the front desk to help with your request."
                )
            return self.say(
                "I'm listening. Are you looking to book or change an appointment, or ask about insurance?"
            )
        if intent == "book" and not recipient_confirmed:
            self.pending_booking = (words, text, meaning)
            self.unclear = 0
            self.stage = "booking_recipient"
            return self.say("Is the appointment for you or for someone else?")
        if self.frontdesk and not self.booking_for_other and (name := opening_name(text)):
            self.name = name
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
        if meaning and meaning.action in {"book", "reschedule"} and meaning.value:
            await self._update_preferences(meaning.value.casefold())
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
        if identity and not self.booking_for_other:
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

    def _clinic_answer(self, action: str) -> str:
        clinic = self.scheduling.clinic
        if action == "hours":
            return f"The clinic is open {clinic.weekday_hours}."
        if action == "locations":
            return self._locations()
        return (
            f"The clinic lists {', '.join(clinic.accepted_payers)}. Staff can confirm other plans."
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
        if self.booking_for_other:
            return self.say(
                "The front desk can help with the patient's waitlist. "
                "Would you like to choose an appointment time?"
            )
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
        return self.say(
            "What's the patient's full name?"
            if self.booking_for_other
            else "What name should I use?"
        )

    def _capture_name(self, text: str, *, confirm: bool = False) -> Reply:
        if self.frontdesk and (identity := self._identity_details(text)):
            supplied_dates = birth_dates(text[identity.end() :], self.scheduling.now().date())
            supplied_dob = supplied_dates[0] if len(supplied_dates) == 1 else None
            if supplied_dob and supplied_dob <= self.scheduling.now().date():
                self.dob = supplied_dob
            text = text[: identity.start()].rstrip(" ,.;")
            text = re.sub(r"\s+(?:and\s+)?my$", "", text, flags=re.I)
            if match := re.search(r"\b(?:my name is|this is)\b", text, re.I):
                text = text[match.start() :]
        name = name_from_reply(text)
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
        return self.say(
            "What's the patient's date of birth?"
            if self.booking_for_other
            else "Thanks. What's your date of birth?"
        )

    def _set_window(self, words: str) -> bool:
        today = self.scheduling.now().astimezone(self.scheduling.clinic.timezone).date()
        days = requested_days(words, today)
        day = days[0] if len(days) == 1 else None
        requested_time = parse_time(words)
        if requested_time is None and re.search(
            r"\b(?:near|closer to|around|about|at|by)\s+(?:lunchtime|lunch)\b|^lunchtime$", words
        ):
            requested_time = time(12)
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
        elif not days and re.search(r"\b(?:next|(?:the )?coming|following) week\b", words):
            week = today + timedelta(days=7 - today.weekday())
        elif not days and re.search(r"\b(?:next|coming) (?:seven|7) days\b", words):
            week = today
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

    def _read_identity(self, prefix: str = "") -> Reply:
        assert self.dob is not None
        return self.say(
            f"{prefix}I heard {self.name}, born {self.dob:%B} {self.dob.day}, {self.dob.year}. "
            "Is that correct?",
            confirm=True,
        )

    def _correct_identity(self, text: str, words: str) -> Reply:
        dates = birth_dates(text, self.scheduling.now().date())
        corrected_date = dates[0] if len(dates) == 1 else None
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
        if introduces_name(text) or (
            self.stage == "correct_identity" and answer_to_confirmation(words) is None
        ):
            return self._capture_name(text)
        return self._read_identity()

    def _apply_identity_correction(self, text: str, words: str, action: str, value: str) -> Reply:
        if action == "name" and value and value.casefold() in text.casefold():
            return self._capture_name(value, confirm=True)
        if (
            action == "birth_date"
            and (corrected := parse_date(value))
            and corrected <= self.scheduling.now().date()
        ):
            return self._correct_identity(value, words)
        if action == "change_name":
            return self._correct_identity(text, "change my name")
        if action == "change_date":
            return self._correct_identity(text, "date of birth")
        return self._correct_identity(text, words) if self.dob else self._ask_name()

    async def _verify(self) -> Reply:
        if not self.played and self.voice and self.dob is not None:
            self.identity_playback_retries += 1
            if self.identity_playback_retries >= 3:
                return self._handoff(
                    "identity_unverified",
                    "I have the date, but I couldn't confirm the read-back. The front desk can help.",
                )
            return self._read_identity(
                "I have the date. Please wait for the read-back to finish before saying yes. "
            )
        if not self.played or self.dob is None:
            return self._read_identity()
        self.identity_playback_retries = 0
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
        self.options = await self.scheduling.appointments(
            self._patient(), limit=None if self.voice else 2
        )
        if self.voice:
            self.options, self.existing_options = self.options[:2], self.options[2:]
        if not self.options:
            return self.say("I found no upcoming appointments to change.", outcome="info_only")
        self.stage = "select_existing"
        return self._list_options("Which existing appointment do you want to change?")

    def _list_options(self, lead: str) -> Reply:
        options = " ".join(
            f"Option {index}: {label}." for index, label in enumerate(self._offer_labels(), 1)
        )
        return self.say(
            f"{lead} {options}".strip(), calendar=self._calendar(self.options, "offered")
        )

    def _offer_labels(self) -> list[str]:
        if not self.voice:
            return [self.describe(item) for item in self.options]
        labels = []
        clinic = self.scheduling.clinic
        locations = {self.scheduling.location_for(item) for item in self.options}
        providers = {self.scheduling.provider_for(item) for item in self.options}
        local_starts = [instant(item["start"]).astimezone(clinic.timezone) for item in self.options]
        # Different clinicians or locations can be the only distinction at a
        # shared time. Include enough information to choose in that case.
        same_time = len(set(local_starts)) < len(local_starts)
        for item, start in zip(self.options, local_starts, strict=True):
            label = f"{start:%A, %B} {start.day} at {spoken_time(start.time())}"
            if (
                self.stage == "select_existing"
                or same_time
                or (self.provider and len(providers) > 1)
            ):
                label += f" with {clinic.providers.get(self.scheduling.provider_for(item), 'the clinic')}"
            if (self.location or same_time or self.stage == "select_existing") and len(
                locations
            ) > 1:
                if location := clinic.locations.get(self.scheduling.location_for(item)):
                    label += f" at {location}"
            labels.append(label)
        return labels

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
            limit=2 if self.voice else 3,
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
            requested = (
                spoken_time(self.requested_time)
                if self.voice
                else self.requested_time.strftime("%I:%M %p").lstrip("0")
            )
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

    def _offer_descriptions(self) -> list[str]:
        zone = self.scheduling.clinic.timezone
        starts = [instant(option["start"]).astimezone(zone) for option in self.options]
        chronological = sorted(set(starts))
        labels = (
            ["only"]
            if len(chronological) == 1
            else ["earliest", *(["intermediate"] * max(0, len(chronological) - 2)), "latest"]
        )
        return [
            f"{self.describe(option)} ({'morning' if start.hour < 12 else 'afternoon'}, "
            f"{labels[chronological.index(start)]} by time)"
            for option, start in zip(self.options, starts, strict=True)
        ]

    def _relative_option(self, words: str) -> tuple[bool, Resource | None]:
        reference = re.fullmatch(
            r"(?:(?:i'll take|i want|i'd like|let's do|go with) )?(?:the )?"
            r"(earlier|earliest|later|latest)(?: one| appointment| time)?"
            r"(?: of (?:the|those|these) (?:two|three))?(?: please| works(?: for me)?)?",
            words,
        )
        if not reference or not self.options:
            return False, None
        rank = min if reference[1] in {"earlier", "earliest"} else max
        chosen = rank(instant(option["start"]) for option in self.options)
        matches = [option for option in self.options if instant(option["start"]) == chosen]
        return True, matches[0] if len(matches) == 1 else None

    def _choose_option(
        self, text: str, words: str, *, existing: bool = False, meaning: TurnMeaning | None = None
    ) -> Resource | None:
        if direct := self._selection(words):
            return direct
        relative, option = self._relative_option(words)
        if relative:
            return option
        if not existing and (by_time := self._offered_time(text, words)):
            return by_time
        if meaning and meaning.action == "select":
            if text.rstrip().endswith("?") or re.search(
                r"\b(?:maybe|either|neither|both|instead|closer to|doesn't work|won't work|can't do)\b",
                words,
            ):
                return None
            index = int(meaning.value) - 1 if re.fullmatch(r"[1-9][0-9]?", meaning.value) else -1
            if 0 <= index < len(self.options):
                return self.options[index]
        return None

    async def _select_existing(
        self, text: str, words: str, meaning: TurnMeaning | None = None
    ) -> Reply:
        if self.voice and (MORE_TIMES.search(words) or (meaning and meaning.action == "more")):
            if self.existing_options:
                self.options, self.existing_options = (
                    self.existing_options[:2],
                    self.existing_options[2:],
                )
                return self._list_options("Here are more appointments.")
            return self._list_options("These are the last appointments I found.")
        self.old = self._choose_option(text, words, existing=True, meaning=meaning)
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
                visit = (
                    f"the patient's {self.visit}"
                    if self.booking_for_other
                    else f"your {self.visit}"
                )
                text = f"Book {visit} on {time}?"
                if change.new_patient is not None:
                    text = f"Create a new demo patient record and book {visit} on {time}?"
        return self.say(
            prefix + text + " Please say yes or no.",
            confirm=True,
            calendar=self._calendar([change.hold.slot], "held") if change.hold else (),
        )

    async def _prepare_confirmation(self) -> Reply:
        if self.frontdesk and not self.email_checked and not self.booking_for_other:
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

    async def _confirm(self, words: str, meaning: TurnMeaning | None = None) -> Reply:
        action = meaning.action if meaning else ""
        if action == "email_question" and self.intent != "cancel" and not self.booking_for_other:
            message = (
                "I'll use the email address we confirmed if you go ahead."
                if self.email_on_file
                else "The front desk will need an email address to send a confirmation."
            )
            return self._read_change(message + " ")
        if (
            self.frontdesk
            and not self.booking_for_other
            and self.intent != "cancel"
            and (
                action == "change_email"
                or re.fullmatch(
                    r"(?:please )?(?:change|update|use a different) (?:my |the )?email(?: address)?",
                    words,
                )
            )
        ):
            self.stage = "email_address"
            return self.say("What email address should I use for this appointment confirmation?")
        answer = self._context_answer(words, meaning, change=True)
        if (
            answer == "no"
            or re.match(
                r"(?:please )?(?:i (?:don't|do not) want you to |don't |do not )"
                r"(?:book|schedule|move|reschedule|cancel)\b",
                words,
            )
            and not parse_time(words)
            and not requested_days(
                words, self.scheduling.now().astimezone(self.scheduling.clinic.timezone).date()
            )
        ):
            await self.close()
            return self.say("Okay, I made no changes.", outcome="declined")
        confirmed = answer == "yes"
        if self.intent != "cancel" and not confirmed and await self._update_preferences(words):
            await self.close()
            return await self._offer()
        if (
            self.intent != "cancel"
            and meaning
            and action == "preferences"
            and await self._update_preferences(meaning.value.casefold())
        ):
            await self.close()
            return await self._offer()
        if self.intent != "cancel" and (action == "more" or MORE_TIMES.search(words)):
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
        if self.frontdesk and not self.booking_for_other:
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
            if self.frontdesk and not self.booking_for_other:
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
                (
                    f"The patient is booked for {self.describe(change.hold.slot)}."
                    if self.booking_for_other
                    else f"You're {outcome} for {self.describe(change.hold.slot)}."
                )
                + email_note,
                outcome=outcome,
                calendar=self._calendar([change.hold.slot], "booked"),
                email_status=delivery if self.email_form else None,
            )
        return self.say("Your appointment was cancelled.", outcome=outcome)
