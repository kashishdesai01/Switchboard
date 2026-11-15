"""Safety assessment and model interpretation of a turn."""

from __future__ import annotations

import asyncio

from switchboard.call.vocabulary import (
    CONFIRMATION_STAGES,
    TURN_ACTIONS,
    Reply,
)
from switchboard.intents import TurnMeaning
from switchboard.safety import (
    ACUTE_REASONS,
    ACUTE_SCRIPTS,
    URGENT_SCRIPT,
    Level,
    Triage,
    triage,
)
from switchboard.understanding import (
    answer_to_confirmation,
    qualified,
)


class SafetyMixin:
    def emergency_reply(
        self, text: str | None = None, *, result: Triage | None = None, reason: str | None = None
    ) -> Reply:
        """Give the script for an emergency, crisis or poisoning and end the call.

        ``text`` is a caller turn not yet recorded, such as a barge-in. Its triage
        picks the script when no result is given. Anything else gets the 911 script.
        """
        if text and self.trace:
            self.trace.record("caller", text)
        if result is None and text:
            result = triage(text)
        if result is None or not result.level.acute:
            result = Triage(Level.EMERGENCY, result.category if result else None)
        # Emergency work replaces any earlier staff work for this call.
        self._work(reason or ACUTE_REASONS[result.level], result)
        reply = self.say(ACUTE_SCRIPTS[result.level], outcome="emergency")
        if self.trace:
            self.trace.add(
                "emergency_routed",
                state=self.stage,
                level=result.level.value,
                category=result.category or "unspecified",
            )
        return reply

    async def urgent_reply(self, result: Triage) -> Reply:
        """Same-day symptoms go to the clinical team, never to a routine booking."""
        await self.close()
        self.options = []
        self._work("urgent_symptoms", result)
        reply = self.say(URGENT_SCRIPT, outcome="handoff")
        if self.trace:
            self.trace.add(
                "urgent_routed",
                state=self.stage,
                level=result.level.value,
                category=result.category or "unspecified",
            )
        return reply

    async def _assess_safety(self, text: str) -> Level | None:
        assessor = getattr(self.interpreter, "assess_safety", None)
        if assessor is None:
            return None
        try:
            async with asyncio.timeout(8):
                level = await assessor(text, list(self.history)[-3:])
        except Exception:  # noqa: BLE001 - any model failure falls back to the rules
            level = None
        if self.trace:
            self.trace.add("safety_assessed" if level is not None else "safety_unassessed")
        return level if isinstance(level, Level) else None

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
