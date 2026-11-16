"""Shared vocabulary of a call: patterns, stage names, and reply types."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from switchboard.intents import GENERAL_ACTIONS as GENERAL_ACTIONS
from switchboard.intents import INFO_ACTIONS as INFO_ACTIONS
from switchboard.safety import ACUTE_REASONS
from switchboard.understanding import answer_to_confirmation as answer_to_confirmation

LOG = logging.getLogger("switchboard.conversation")

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
    "name": {
        "name": "The caller states a person's name. Value is only that name. "
        "A reply without a person's name is not this action"
    },
    "dob": {
        "birth_date": "Normalize the stated birth date to YYYY-MM-DD, Month DD, or YYYY alone",
        "keypad": "Caller asks to enter the date on the phone keypad",
        "name": "Caller gives or corrects the patient's name instead of a birth date. "
        "Value is only that name",
    },
    "correct_identity": {
        "name": "Caller gives a different name from the one in the question. Value is that name",
        "birth_date": "Caller gives a different birth date from the one in the question. "
        "Value is it as YYYY-MM-DD",
        "change_name": "Caller asks to correct their name but does not give the replacement",
        "change_date": "Caller asks to correct their birth date but does not give the replacement",
    },
    "email_address": {
        "email": "Caller states an email address. Value is it normalized, including spoken at "
        "and dot. Not the address in the question",
        "skip_email": "Caller does not want to give any email address",
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
# Stages whose readings may name a provider, place or service.
CLINIC_DETAIL_STAGES = frozenset({"intent", "anything_else", "other_provider"})
TURN_ACTIONS["anything_else"] = {
    **TURN_ACTIONS["intent"],
    "done": "The caller has nothing else to ask and is ending the call",
}
# Outcomes that finish one task. The call goes on to "anything else?".
# Every other outcome (emergency, handoff, needs_review, closed, goodbye) ends the call.
TASK_OUTCOMES = frozenset(
    {"info_only", "booked", "rescheduled", "cancelled", "declined", "waitlisted"}
)
ANYTHING_ELSE = " Is there anything else I can help with?"
MAX_TASKS = 5
# Work items that a later task must never replace.
PRIORITY_WORK = frozenset({*ACUTE_REASONS.values(), "possible_emergency", "urgent_symptoms"})
_FAREWELL = (
    r"no|nope|nah|no thanks?|no thank you|thanks?|thank you|thanks so much|"
    r"nothing(?: else)?|that's (?:all|it|everything)|that is (?:all|it|everything)|"
    r"i'm (?:good|done|all set)|i am (?:good|done|all set)|all set|(?:good ?)?bye|"
    r"have a (?:good|nice|great) (?:day|one)"
)
GOODBYE = re.compile(rf"(?:{_FAREWELL})(?: (?:{_FAREWELL}))*")


def is_goodbye(text: str) -> bool:
    words = " ".join(re.sub(r"[^\w' ]", " ", text.lower().replace("’", "'")).split())
    return bool(words) and bool(GOODBYE.fullmatch(words))


CONFIRMATION_STAGES = {
    "check_name",
    "check_dob",
    "email_readback",
    "confirm_change",
    "other_provider",
    "day_alternative",
    "proxy_permission",
}


@dataclass
class Party:
    """Who the call is about. The caller is not always the patient.

    patient is "self" when the caller is the patient, "other" when they act for
    someone else, and "unknown" until the opening or an answer settles it.
    """

    patient: Literal["self", "other", "unknown"] = "unknown"
    relation: str | None = None
    name: str | None = None

    @property
    def proxy(self) -> bool:
        return self.patient == "other"


PROXY_ACTIONS = {
    "book": ("book a visit for this patient", "booking"),
    "reschedule": ("change this patient's appointment", "change"),
    "cancel": ("cancel this patient's appointment", "cancellation"),
}


@dataclass(frozen=True)
class Reply:
    text: str
    id: str
    outcome: str | None = None
    confirmation: bool = False
    calendar: tuple[dict, ...] = ()
    email_status: str | None = None
    ends_call: bool = False
