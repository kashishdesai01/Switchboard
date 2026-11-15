"""Reading a change back, capturing email, and committing it."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace

from switchboard.call.vocabulary import (
    LOG,
    MORE_TIMES,
    Reply,
)
from switchboard.frontdesk import email_address
from switchboard.intents import TurnMeaning
from switchboard.scheduling import instant
from switchboard.spoken import (
    parse_time,
    requested_days,
)
from switchboard.store import Conflict, Unavailable


class ConfirmationMixin:
    def _read_change(self, prefix: str = "") -> Reply:
        change = self.change
        assert change is not None
        whose = "the patient's" if self.party.proxy else "your"
        if change.action == "cancel":
            assert change.old is not None
            text = f"Cancel {whose} appointment on {self.describe(change.old)}?"
        else:
            assert change.hold is not None
            time = self.describe(change.hold.slot)
            if change.action == "reschedule":
                assert change.old is not None
                text = f"Move {whose} appointment from {self.describe(change.old)} to {time}?"
            else:
                visit = f"{whose} {self.visit}"
                text = f"Book {visit} on {time}?"
                if change.new_patient is not None:
                    text = f"Create a new demo patient record and book {visit} on {time}?"
        return self.say(
            prefix + text + " Please say yes or no.",
            confirm=True,
            calendar=self._calendar([change.hold.slot], "held") if change.hold else (),
        )

    async def _prepare_confirmation(self) -> Reply:
        if self.frontdesk and not self.email_checked and not self.party.proxy:
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
        if action == "email_question" and self.intent != "cancel" and not self.party.proxy:
            message = (
                "I'll use the email address we confirmed if you go ahead."
                if self.email_on_file
                else "The front desk will need an email address to send a confirmation."
            )
            return self._read_change(message + " ")
        if (
            self.frontdesk
            and not self.party.proxy
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
        if self.frontdesk and not self.party.proxy:
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
            if self.frontdesk and not self.party.proxy:
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
                    f"The patient is {outcome} for {self.describe(change.hold.slot)}."
                    if self.party.proxy
                    else f"You're {outcome} for {self.describe(change.hold.slot)}."
                )
                + email_note,
                outcome=outcome,
                calendar=self._calendar([change.hold.slot], "booked"),
                email_status=delivery if self.email_form else None,
            )
        return self.say(
            "The patient's appointment was cancelled."
            if self.party.proxy
            else "Your appointment was cancelled.",
            outcome=outcome,
        )
