"""Name and birth-date capture, read-back, correction, and verification."""

from __future__ import annotations

import re

from switchboard.call.vocabulary import (
    Reply,
)
from switchboard.spoken import (
    birth_dates,
    parse_date,
)
from switchboard.understanding import (
    answer_to_confirmation,
    introduces_name,
    name_from_reply,
)


class IdentityMixin:
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

    async def _identify(self) -> Reply:
        """Ask who the patient is, unless the caller already verified as the patient."""
        if self.known_patient and not self.party.proxy:
            self.party.patient = "self"
            self.patient = self.known_patient
            self.name, self.dob = "", None
            return await self._after_verify()
        return self._ask_name()

    def _ask_name(self) -> Reply:
        if self.name:
            self.stage = "check_name"
            if self.party.proxy:
                return self.say(
                    f"I heard the patient's name as {self.name}. Is that correct? "
                    "You can also say change the name.",
                    confirm=True,
                )
            return self.say(
                f"I heard your name as {self.name}. Is that correct? You can also say change my name.",
                confirm=True,
            )
        self.stage = "name"
        return self.say(
            "What's the patient's full name?" if self.party.proxy else "What name should I use?"
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
            if self.party.proxy
            else "Thanks. What's your date of birth?"
        )

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
            # The caller talked over the read-back, so say it again in full.
            return self._read_identity("Sorry, let me say that again. ")
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
            return self.say(
                "I couldn't verify those details. Please repeat the patient's full name."
                if self.party.proxy
                else "I couldn't verify those details. Please repeat your full name."
            )
        return await self._after_verify()

    async def _after_verify(self) -> Reply:
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
        appointments = await self.scheduling.appointments(self._patient(), limit=None)
        page = self._existing_page()
        self.options, self.existing_options = appointments[:page], appointments[page:]
        self.single_existing = len(appointments) == 1
        if not self.options:
            return self.say("I found no upcoming appointments to change.", outcome="info_only")
        self.stage = "select_existing"
        if self.single_existing:
            return self._read_single_existing()
        return self._list_options(
            "Which of the patient's appointments do you want to change?"
            if self.party.proxy
            else "Which existing appointment do you want to change?"
        )
