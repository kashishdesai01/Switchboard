"""Opening a request: proxy checks, preferences, services, and providers."""

from __future__ import annotations

import re
from datetime import time, timedelta

from switchboard.call.vocabulary import (
    PROVIDER_NON_NAMES,
    PROVIDER_TITLE,
    PROXY_ACTIONS,
    Party,
    Reply,
)
from switchboard.intents import TurnMeaning
from switchboard.spoken import (
    parse_time,
    requested_days,
)
from switchboard.understanding import (
    Proxy,
    booking_recipient,
    is_booking_request,
    is_greeting,
    opening_name,
    request_route,
)


class IntakeMixin:
    @staticmethod
    def _says_self(text: str, meaning: TurnMeaning | None) -> bool:
        """Whether the opening request says plainly that the caller is the patient.

        The model reading decides; the reply parser answers only without a reading.
        """
        if meaning is not None:
            return meaning.patient == "self" and not meaning.doubt
        return booking_recipient(text).kind == "self"

    def _proxy_action(
        self,
        words: str,
        meaning: TurnMeaning | None,
        proxy: Proxy | None,
        booking: bool,
        staff: re.Match | None,
    ) -> str | None:
        """The request an opening makes for someone else: book, reschedule or cancel."""
        if self.stage != "intent" or not proxy:
            return None
        if booking:
            return "book"
        if staff:
            return None
        route = (
            meaning.action
            if meaning and meaning.action in {"reschedule", "cancel"}
            else request_route(words)
        )
        return route if route in {"reschedule", "cancel"} else None

    def _ask_proxy_permission(self, prefix: str = "") -> Reply:
        self.stage = "proxy_permission"
        self.unclear = 0
        if self.proxy_allowed:
            return self.say(f"{prefix}Do you have permission to {PROXY_ACTIONS[self.intent][0]}?")
        self.staff_topic = "proxy request"
        if self.intent == "book":
            return self.say(
                "The front desk can help book visits for someone else. "
                "Would you like me to send them your scheduling request?"
            )
        return self.say(
            "The front desk can help change appointments for someone else. "
            f"Would you like me to send them your {PROXY_ACTIONS[self.intent][1]} request?"
        )

    async def _start(
        self,
        words: str,
        text: str,
        meaning: TurnMeaning | None = None,
        *,
        patient_confirmed: bool = False,
    ) -> Reply:
        if not patient_confirmed:
            if is_greeting(text):
                # "Hi." or "Hello, how are you?" asks for nothing yet, whatever a model
                # reads into it: answer in kind and ask how to help.
                return self.say("Hi there. How can I help today?")
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
                return await self._identify()
        intent = (
            self.intent
            if patient_confirmed
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
            if self.frontdesk and not self.party.proxy and (name := opening_name(text)):
                self.name = name
            self.unclear += 1
            if self.frontdesk and self.unclear >= 3:
                return self._handoff(
                    "staff_requested", "I'll ask the front desk to help with your request."
                )
            return self.say(
                "I'm listening. Are you looking to book or change an appointment, or ask about insurance?"
            )
        if intent == "book" and not patient_confirmed and self._says_self(text, meaning):
            # "Book a checkup for myself": the caller already said who the patient is.
            self.party = Party("self")
            patient_confirmed = True
        if intent == "book" and not patient_confirmed:
            self.intent = "book"
            self.pending_request = (words, text, meaning)
            self.unclear = 0
            self.stage = "booking_recipient"
            return self.say("Is the appointment for you or for someone else?")
        if self.frontdesk and not self.party.proxy and (name := opening_name(text)):
            self.name = name
        self.intent = intent
        if not self.party.proxy:
            # Without a proxy cue the caller is the patient, as before.
            self.party.patient = "self"
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
        if identity and not self.party.proxy and not self.known_patient:
            self.stage = "name"
            return self._capture_name(text[identity_start:])
        return await self._identify()

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
        if self.party.proxy:
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
