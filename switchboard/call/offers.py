"""Offering times and existing appointments, and choosing among them."""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import uuid4

from switchboard.call.vocabulary import (
    MORE_TIMES,
    Reply,
)
from switchboard.intents import TurnMeaning
from switchboard.scheduling import Change, instant
from switchboard.spoken import (
    parse_time,
    requested_days,
    spoken_time,
)
from switchboard.store import Resource
from switchboard.understanding import (
    answer_to_confirmation,
)


class OffersMixin:
    def _existing_page(self) -> int:
        return 2 if self.voice else 3

    def _read_single_existing(self, prefix: str = "") -> Reply:
        whose = "The patient has" if self.party.proxy else "You have"
        action = self.intent if self.intent in {"reschedule", "cancel"} else "change"
        return self.say(
            f"{prefix}{whose} one upcoming appointment: {self.describe(self.options[0])}. "
            f"Do you want to {action} it?",
            calendar=self._calendar(self.options, "offered"),
        )

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
            # A new-patient draft has no appointments to avoid.
            patient=None if self.new_patient else self.patient,
            ignore=self.old["id"] if self.old else None,
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
        more = (
            MORE_TIMES.search(words)
            or re.fullmatch(r"(?:(?:any|some) )?more(?: appointments)?(?: please)?", words)
            or (meaning and meaning.action == "more")
        )
        if self.single_existing:
            if more:
                return self._read_single_existing("These are the last appointments I found. ")
            answer = answer_to_confirmation(words, self.intent) or self._context_answer(
                words, meaning
            )
            if answer == "no":
                return self.say("Okay. I haven't made any changes.", outcome="declined")
            # "One", "option one" and "the first one" name the only appointment.
            if answer == "yes" or self._selection(words) is not None:
                self.old = self.options[0]
            else:
                return self._read_single_existing("Please say yes or no. ")
        else:
            if more:
                if self.existing_options:
                    page = self._existing_page()
                    self.options, self.existing_options = (
                        self.existing_options[:page],
                        self.existing_options[page:],
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
