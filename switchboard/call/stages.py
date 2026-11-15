"""Per-stage turn handling: one method per conversation stage."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import ClassVar

from switchboard.call.vocabulary import (
    MORE_TIMES,
    PROXY_ACTIONS,
    TURN_ACTIONS,
    Party,
    Reply,
)
from switchboard.frontdesk import email_address
from switchboard.intents import TurnMeaning
from switchboard.scheduling import Change
from switchboard.spoken import (
    MONTH_PATTERN,
    birth_dates,
    date_words,
    parse_date,
)
from switchboard.understanding import (
    answer_to_confirmation,
    booking_recipient,
    model_says_self,
    proxy_request,
    states_name,
)


class StagesMixin:
    async def _handle(self, text: str, words: str, meaning: TurnMeaning | None = None) -> Reply:
        answer = self._context_answer(words, meaning)
        action = meaning.action if meaning else ""
        value = meaning.value.strip() if meaning else ""
        handler = self.STAGES.get(self.stage)
        if handler is None:
            raise RuntimeError(f"Invalid conversation stage: {self.stage}")
        return await handler(self, text, words, meaning, answer, action, value)

    async def _stage_intent(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        return await self._start(words, text, meaning)

    async def _stage_booking_recipient(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        recipient = booking_recipient(text)
        if action == "other" or recipient.kind == "other":
            said = proxy_request(words)
            self.party = Party("other", said.relation if said else None, recipient.name)
            if not self.proxy_allowed:
                self.pending_request = None
            return self._ask_proxy_permission()
        if recipient.kind == "self" or (action == "self" and model_says_self(text, value)):
            assert self.pending_request is not None
            opening_words, opening_text, opening_meaning = self.pending_request
            self.pending_request = None
            self.party = Party("self")
            return await self._start(
                opening_words, opening_text, opening_meaning, patient_confirmed=True
            )
        self.unclear += 1
        if self.unclear >= 3:
            self.pending_request = None
            return self._handoff(
                "staff_requested",
                "I couldn't confirm who the appointment is for. The front desk can help.",
            )
        return self.say("Is the appointment for you or for someone else?")

    async def _stage_proxy_permission(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        if self.proxy_allowed:
            if answer_to_confirmation(words) == "no":
                self.pending_request = None
                return self.say("Okay. I haven't made any changes.", outcome="declined")
            if answer_to_confirmation(words, self.intent) == "yes":
                assert self.pending_request is not None
                opening_words, opening_text, opening_meaning = self.pending_request
                self.pending_request = None
                # Identity questions now refer to the patient, not the caller.
                self.name, self.dob = self.party.name or "", None
                return await self._start(
                    opening_words, opening_text, opening_meaning, patient_confirmed=True
                )
            self.unclear += 1
            if self.unclear >= 3:
                self.pending_request = None
                return self._handoff(
                    "staff_requested",
                    "I couldn't confirm permission for this patient. The front desk can help.",
                )
            return self.say(
                f"Do you have permission to {PROXY_ACTIONS[self.intent][0]}? Please say yes or no."
            )
        request = PROXY_ACTIONS[self.intent][1]
        if answer == "yes":
            return self._handoff(
                "staff_requested", f"I'll pass your {request} request to the front desk."
            )
        if answer == "no":
            return self.say("Okay. I haven't sent a request.", outcome="declined")
        if self.intent != "cancel" and await self._update_preferences(words):
            return self.say(
                "I have your scheduling preferences. Should I send this request to the front desk?"
            )
        self.unclear += 1
        if self.unclear >= 3:
            return self._handoff(
                "staff_requested",
                "I couldn't confirm your answer. The front desk can help.",
            )
        return self.say(f"Should I send the front desk your {request} request?")

    async def _stage_other_provider(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        self.provider = self._provider(words) or (
            self._provider(value.casefold()) if action == "preferences" else None
        )
        if self.provider or answer == "yes" or words in {"anyone", "another doctor"}:
            self._set_window(words)
            return await self._identify()
        if answer == "no":
            return self.say("Of course. I haven't booked anything.", outcome="declined")
        return self.say("Would you like to hear times with another doctor here?")

    async def _stage_name(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        if (
            action == "name"
            and value
            and value.casefold() in text.casefold()
            and not self._identity_details(text)
        ):
            return self._capture_name(value)
        return self._capture_name(text)

    async def _stage_check_name(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        if answer == "yes" and self.played:
            self.stage = "dob"
            return self.say(
                "What's the patient's date of birth?"
                if self.party.proxy
                else "Thanks. What's your date of birth?"
            )
        if answer == "no" or words in {
            "change it",
            "change my name",
            "change the name",
            "the name",
        }:
            self.name = ""
            return self._ask_name()
        if states_name(text):
            return self._capture_name(text, confirm=True)
        if action in TURN_ACTIONS["correct_identity"]:
            return self._apply_identity_correction(text, words, action, value)
        return self._ask_name()

    async def _stage_day_alternative(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
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

    async def _stage_dob(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
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

    async def _stage_keypad(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        return self.say("Enter eight digits on the keypad, or press zero twice for staff.")

    async def _stage_check_dob(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        if answer == "no":
            self.stage = "correct_identity"
            return self.say("Of course. Which should I change, the name or date of birth?")
        if answer != "yes":
            if action in TURN_ACTIONS["correct_identity"]:
                return self._apply_identity_correction(text, words, action, value)
            return self._correct_identity(text, words)
        return await self._verify()

    async def _stage_correct_identity(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        return self._apply_identity_correction(text, words, action, value)

    async def _stage_select_existing(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        return await self._select_existing(text, words, meaning)

    async def _stage_select_slot(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        if MORE_TIMES.search(words):
            return await self._offer(more=True)
        if re.search(r"\b(?:closer to|near|around)\b", words) and await self._update_preferences(
            words
        ):
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

    async def _stage_confirm_change(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        return await self._confirm(words, meaning)

    async def _stage_email_address(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        if action == "skip_email":
            return self._capture_email(text, "skip email")
        if action == "email" and email_address(value):
            return self._capture_email(value, words)
        return self._capture_email(text, words)

    async def _stage_email_readback(
        self,
        text: str,
        words: str,
        meaning: TurnMeaning | None,
        answer: str | None,
        action: str,
        value: str,
    ) -> Reply:
        if answer == "yes" and self.played:
            self.email_on_file, self.pending_email = self.pending_email, None
            self.stage = "confirm_change"
            return self._read_change("Thank you. I'll use that address for the confirmation. ")
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

    STAGES: ClassVar[dict[str, Callable[..., Awaitable[Reply]]]] = {
        "intent": _stage_intent,
        "booking_recipient": _stage_booking_recipient,
        "proxy_permission": _stage_proxy_permission,
        "other_provider": _stage_other_provider,
        "name": _stage_name,
        "check_name": _stage_check_name,
        "day_alternative": _stage_day_alternative,
        "dob": _stage_dob,
        "keypad": _stage_keypad,
        "check_dob": _stage_check_dob,
        "correct_identity": _stage_correct_identity,
        "select_existing": _stage_select_existing,
        "select_slot": _stage_select_slot,
        "confirm_change": _stage_confirm_change,
        "email_address": _stage_email_address,
        "email_readback": _stage_email_readback,
    }
