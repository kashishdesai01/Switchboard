"""Safety assessment and model interpretation of a turn."""

from __future__ import annotations

import asyncio

from switchboard.call.vocabulary import (
    CLINIC_DETAIL_STAGES,
    CONFIRMATION_STAGES,
    GENERAL_ACTIONS,
    INFO_ACTIONS,
    TURN_ACTIONS,
    Reply,
)
from switchboard.intents import NO_ACTION, NO_ACTION_DESCRIPTION, TurnMeaning, settle
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
    bare_acknowledgment,
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
        """Read the caller's reply against the question just asked.

        The reading's yes/no ``answer`` reaches the stages as the affirm and decline
        actions (see ``settle``), so ``_context_answer`` keeps one yes/no path.
        """
        if not self.interpreter:
            return None
        actions = dict(TURN_ACTIONS.get(self.stage, {}))
        if self.safety_resume:
            actions["current_danger"] = (
                "Caller explicitly confirms the possible emergency is happening now."
            )
            actions["historical"] = (
                "Caller explicitly says the condition is historical, resolved, or hypothetical."
            )
        if self.stage in {"check_name", "check_dob", "email_readback"}:
            actions.update(
                TURN_ACTIONS["correct_identity"]
                if self.stage != "email_readback"
                else TURN_ACTIONS["email_address"]
            )
        # Elsewhere a staff request is flagged by needs_staff: offered as an action it
        # draws readings away from the answer. Clinic questions stay answerable mid-call,
        # except on yes/no questions, where offering them pulls plain answers away.
        if self.stage in {"intent", "anything_else"}:
            actions.update(GENERAL_ACTIONS)
        elif self.stage not in CONFIRMATION_STAGES:
            actions.update(INFO_ACTIONS)
        if self.stage in CONFIRMATION_STAGES:
            # A bare yes or no reaches the stage as affirm or decline (see settle).
            actions[NO_ACTION] = NO_ACTION_DESCRIPTION
        context = {
            "stage": self.stage,
            "request": self.intent,
            # The question is the latest assistant turn; recent turns come before it.
            "recent": [
                {who: said[:160] for who, said in turn.items()}
                for turn in list(self.history)[-3:-1]
            ],
            "caller": text,
        }
        if self.last_reply:
            context["question"] = self.last_reply.text[-300:]
        clinic = self.scheduling.clinic
        context["clinic"] = {"today": str(self.scheduling.now().astimezone(clinic.timezone).date())}
        if self.stage in CLINIC_DETAIL_STAGES:
            context["clinic"].update(
                providers=list(clinic.providers.values()),
                locations=list(clinic.locations.values()),
                services=clinic.referral_services,
            )
        if self.stage in {"select_slot", "select_existing"}:
            # The options carry the current offer. Avoid repeating it in the history.
            context.pop("recent", None)
            if self.last_reply:
                context["question"] = self.last_reply.text.split("Option 1:", 1)[0].strip()[:200]
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
        if result is not None:
            result = settle(result, self.stage in CONFIRMATION_STAGES, context.get("question", ""))
        if self.trace:
            self.trace.add(
                "context_interpreted" if result else "context_unresolved", state=self.stage
            )
        return result

    def _context_answer(
        self, words: str, meaning: TurnMeaning | None, *, change: bool = False
    ) -> str | None:
        """The caller's yes or no to the question just asked, or None.

        The model reading decides; the handwritten parser answers only when there is no
        reading. A doubtful or hedged reading is never a yes. ``change`` asks for consent
        to change an appointment: there the regex cross-check can veto a model yes when
        the words carry a qualification, and a bare "okay" is no consent.
        """
        action = self.intent if change else None
        unread = meaning is not None and (
            meaning.action == "unclear" and meaning.answer == "not_applicable" and not meaning.doubt
        )
        if meaning is None or unread:
            # No reading, or one that gave no answer at all to the question.
            return answer_to_confirmation(words, action)
        if meaning.action == "decline":
            return "no"
        if meaning.action == "affirm" and not meaning.doubt and meaning.answer != "unclear":
            if change and (qualified(words) or bare_acknowledgment(words)):
                return None
            return "yes"
        return None

    def _answers_question(self, meaning: TurnMeaning | None) -> bool:
        """Whether a model reading answers the question the caller was just asked."""
        if meaning is None or self.stage in {"intent", "anything_else"}:
            return False
        if self.stage in CONFIRMATION_STAGES and meaning.action in {"affirm", "decline"}:
            return True
        if meaning.action == "select":
            return bool(self.options)
        stage_actions = set(TURN_ACTIONS.get(self.stage, {})) - set(GENERAL_ACTIONS)
        if self.stage in {"check_name", "check_dob"}:
            stage_actions |= set(TURN_ACTIONS["correct_identity"])
        return meaning.action in stage_actions and (
            bool(meaning.value.strip()) or self.stage not in {"name", "dob"}
        )
