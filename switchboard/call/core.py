"""The Call: one phone call's state, turn loop, and task bookkeeping."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, time
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from switchboard.intents import TurnInterpreter, TurnMeaning
from switchboard.operations import summary_outcome
from switchboard.safety import (
    GREETING_NOTICE,
    Level,
    Triage,
    assess,
    condition_only,
    raise_level,
)
from switchboard.scheduling import Change, Scheduling, instant
from switchboard.spoken import (
    birth_dates,
    spoken_time,
)
from switchboard.store import Conflict, DemoStore, Resource, Unavailable
from switchboard.understanding import (
    answer_to_confirmation,
    is_booking_request,
    leading_answer,
    name_from_reply,
    proxy_request,
    reading_proxy,
    request_route,
)

if TYPE_CHECKING:
    from switchboard.frontdesk import FrontDesk
    from switchboard.operations import Trace
from switchboard.call.confirmation import ConfirmationMixin
from switchboard.call.identity import IdentityMixin
from switchboard.call.intake import IntakeMixin
from switchboard.call.offers import OffersMixin
from switchboard.call.safety_routing import SafetyMixin
from switchboard.call.stages import StagesMixin
from switchboard.call.vocabulary import (
    ANYTHING_ELSE,
    CONFIRMATION_STAGES,
    EXPLICIT_STAFF,
    MAX_TASKS,
    PRIORITY_WORK,
    STAFF_TOPIC,
    TASK_OUTCOMES,
    Party,
    Reply,
    is_goodbye,
)


class Call(StagesMixin, SafetyMixin, IdentityMixin, IntakeMixin, OffersMixin, ConfirmationMixin):
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
        # Changes for someone else are handled here only in the SQLite demo with a
        # front desk. Elsewhere the request goes to staff. Applies to every action.
        self.proxy_allowed = bool(frontdesk and isinstance(scheduling.store, DemoStore))
        self.party = Party()
        self.frontdesk = frontdesk
        self.email_form = email_form
        self.stage = "intent"
        self.safety_resume: Reply | None = None
        self.safety_opening: str | None = None
        self.safety_retries = 0
        self.pending_request: tuple[str, str, TurnMeaning | None] | None = None
        self.patient: str | None = None
        self.name = ""
        self.dob: date | None = None
        self.dob_fragment: str | None = None
        # Leading digits of a birth year cut off mid-reply ("August 18th, 19").
        self.dob_year_start = ""
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
        # Replies in a row that didn't answer the question and drew a staff reading.
        self.off_topic = 0
        # Replies to the name question the model read as holding no name.
        self.nameless_replies = 0
        self.opening_interpreter_failures = 0
        self.digits = ""
        self.provider: str | None = None
        self.options: list[Resource] = []
        self.existing_options: list[Resource] = []
        self.single_existing = False
        self.shown_slots: set[str] = set()
        self.old: Resource | None = None
        self.change: Change | None = None
        self.prompt_id: str | None = None
        self.played = False
        self.history: deque[dict[str, str]] = deque(maxlen=6)
        self.last_reply: Reply | None = None
        # A patient verified as the caller in an earlier task of this call.
        self.known_patient: str | None = None
        self.tasks = 0
        self.task_work = False
        # Each task's outcome in order, as the call's own record (a trace is optional).
        self.outcomes: list[str] = []

    def greeting(self) -> Reply:
        return self.say(
            f"Hi, thanks for calling {self.scheduling.clinic.name}. I'm the automated demo assistant. "
            f"{GREETING_NOTICE} How can I help today?"
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
        ends_call = bool(outcome) and outcome not in TASK_OUTCOMES
        if outcome in TASK_OUTCOMES:
            text += ANYTHING_ELSE
        reply = Reply(text, uuid4().hex, outcome, confirm, calendar, email_status, ends_call)
        self.last_reply = reply
        self.history.append({"assistant": text[:400]})
        if self.trace:
            self.trace.record("assistant", text)
        self.prompt_id, self.played = (reply.id if confirm else None), False
        if outcome:
            if outcome not in {"goodbye", "closed"}:
                self.outcomes.append(outcome)
            if self.trace and outcome not in {"goodbye", "closed"}:
                self.trace.add_outcome(outcome)
                if (
                    self.frontdesk
                    and outcome in {"handoff", "needs_review", "emergency"}
                    and not self.task_work
                ):
                    reason = {
                        "handoff": "policy_review",
                        "needs_review": "appointment_write_uncertain",
                        "emergency": "emergency",
                    }[outcome]
                    self._work(reason)
            if (
                self.staff_topic
                and self.staff_topic != "proxy request"
                and outcome in {"booked", "declined", "abandoned"}
            ):
                self._work("staff_requested")
            if ends_call:
                self.stage = "closed"
            else:
                self.stage = "anything_else"
                self.tasks += 1
                self.task_work = False
        return reply

    def summary_outcome(self) -> str | None:
        """The call's outcome for transports: emergency or handoff first, else the last task."""
        return summary_outcome(self.outcomes)

    def _work(self, reason: str, triaged: Triage | None = None) -> None:
        if self.trace and self.frontdesk:
            context = {"request": self.intent, "stage": self.stage}
            if triaged is not None:
                context["triage_level"] = triaged.level.value
                if triaged.category:
                    context["category"] = triaged.category
            if self.patient and not self.new_patient:
                context["patient_reference"] = f"Patient/{self.patient}"
            context.update({key: value for key, value in self._preferences().items() if value})
            if self.last_operation:
                context["operation"] = self.last_operation
            if self.staff_topic:
                context["topic"] = self.staff_topic
            if self.party.proxy:
                context["patient"] = "other"
                if self.party.relation:
                    context["relation"] = self.party.relation
                if self.party.name:
                    context["patient_name"] = self.party.name
            item = reason, context
            current = self.trace.work
            if current is not None and not self.task_work:
                # Work from an earlier task stays on record. Only emergency or urgent
                # work takes the main slot from it, and nothing takes it from those.
                if reason in PRIORITY_WORK and current[0] not in PRIORITY_WORK:
                    self.trace.more_work.append(current)
                    self.trace.work = item
                else:
                    self.trace.more_work.append(item)
            else:
                self.trace.work = item
            self.task_work = True

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
        rules, ambiguous = assess(text)
        if rules.level.acute:
            return self.emergency_reply(result=rules)
        if self.stage == "closed":
            return self.say("This call has ended.", outcome="closed")
        try:
            try:
                # The model safety check runs beside the turn interpreter. It can only
                # raise the rule-based level; a timeout or error leaves the rules in charge.
                meaning, model = await asyncio.gather(
                    self._interpret_turn(
                        text
                        if safety_clarification is None
                        else f"{text}\nWhen asked if the condition is happening now, the caller answered: {safety_clarification}"
                    ),
                    self._assess_safety(text),
                )
                level = raise_level(rules, model)
                if level.level.acute:
                    return self.emergency_reply(result=level)
                if meaning and meaning.emergency:
                    return self.emergency_reply()
                if level.level is Level.URGENT:
                    return await self.urgent_reply(level)
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
                            self.stage in {"intent", "anything_else"}
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
                        # Two unclear answers: treat it as a possible emergency.
                        await self.close()
                        return self.emergency_reply(reason="possible_emergency")
                if self.safety_resume or (
                    safety_clarification is None
                    and ambiguous
                    and (meaning is None or meaning.action == "unclear" or condition_only(text))
                ):
                    if not self.safety_resume:
                        self.safety_resume = self.last_reply or self.greeting()
                        if self.stage in {"intent", "anything_else"}:
                            self.safety_opening = text
                    return self.say(
                        "Is this happening right now? If it is an emergency, call 911 now."
                    )
                if self.stage == "anything_else":
                    lead = leading_answer(text)
                    if (
                        is_goodbye(text)
                        or (lead.answer == "no" and (not lead.rest or is_goodbye(lead.rest)))
                        or (
                            meaning
                            and meaning.action == "done"
                            and request_route(words) == "unknown"
                            and not is_booking_request(words)
                        )
                    ):
                        return self.say("Thanks for calling. Goodbye.", outcome="goodbye")
                    if lead.answer == "yes" and not lead.rest:
                        return self.say("What else can I help with?")
                    if lead.answer and lead.rest:
                        # "No, but can I also..." or "Yes, I need to cancel": the rest is the request.
                        text = lead.rest
                        words = " ".join(
                            text.lower().replace(",", " ").replace("’", "'").strip(".!? ").split()
                        )
                    await self._new_task()
                    if self.tasks >= MAX_TASKS:
                        self.staff_topic = "additional requests"
                        return self._handoff(
                            "staff_requested",
                            "I've helped with several requests on this call. "
                            "I'll pass you to the front desk for anything else.",
                        )
                staff = STAFF_TOPIC.search(words)
                proxy = proxy_request(words)
                if proxy is None and meaning and meaning.patient == "other":
                    # The reading names someone else as the patient where the regex
                    # cues ("for my mom", "my dad needs") found nothing.
                    proxy = reading_proxy(meaning.extra, meaning.value, words)
                book_request = is_booking_request(words)
                booking = self.stage == "intent" and (
                    book_request or (not staff and bool(meaning and meaning.action == "book"))
                )
                proxy_action = self._proxy_action(words, meaning, proxy, booking, staff)
                if proxy_action and proxy:
                    # Set before any handoff so staff work records the real request.
                    self.intent = proxy_action
                    self.party = Party("other", proxy.relation)
                if (
                    proxy_action
                    and self.frontdesk
                    and words != "0"
                    and not EXPLICIT_STAFF.search(words)
                ):
                    if self.proxy_allowed:
                        self.pending_request = (words, text, meaning)
                    else:
                        self._set_window(words)
                    return self._ask_proxy_permission()
                if proxy and (
                    # A possessive ("my mom's appointment") only starts the flow above, and
                    # a proxy call already in progress may mention the patient again.
                    proxy.form == "possessive" or (self.party.proxy and self.stage != "intent")
                ):
                    proxy = None
                if (
                    self.stage in {"booking_recipient", "proxy_permission"}
                    and words != "0"
                    and not EXPLICIT_STAFF.search(words)
                ):
                    return await self._handle(text, words, meaning)
                # A reply that answers the current question is grounded: a staff topic
                # or needs_staff flag riding along doesn't hand the caller off.
                grounded_reply = self._answers_question(meaning) or (
                    meaning is None
                    and (
                        (
                            self.stage == "dob"
                            and bool(birth_dates(text, self.scheduling.now().date()))
                        )
                        or (self.stage == "name" and bool(name_from_reply(text)))
                        or (
                            self.stage in CONFIRMATION_STAGES
                            and answer_to_confirmation(words) is not None
                        )
                        or (
                            self.stage in {"select_slot", "select_existing"}
                            and self._selection(words) is not None
                        )
                        or (
                            self.stage == "select_existing"
                            and self.single_existing
                            and answer_to_confirmation(words) is not None
                        )
                    )
                )
                model_staff = bool(meaning and (meaning.action == "staff" or meaning.needs_staff))
                if (
                    model_staff
                    and not staff
                    and not grounded_reply
                    and not booking
                    and words != "0"
                    and not EXPLICIT_STAFF.search(words)
                    and not proxy
                    and self.stage not in {"intent", "anything_else"}
                    and self.off_topic < 1
                    and self.last_reply
                ):
                    # The reply didn't answer the question: ask it again in context
                    # before handing off.
                    self.off_topic += 1
                    previous = self.last_reply
                    question = previous.text.removeprefix("Thanks. ")
                    return self.say(
                        f"Thanks. {question}",
                        confirm=previous.confirmation,
                        calendar=previous.calendar,
                        email_status=previous.email_status,
                    )
                if (
                    words == "0"
                    or EXPLICIT_STAFF.search(words)
                    or proxy
                    or ((staff or model_staff) and not grounded_reply and not booking)
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
                if grounded_reply and staff and not self.staff_topic:
                    # Answered, with a staff topic alongside: record it for the front desk.
                    self.staff_topic = staff[0]
                if grounded_reply and not staff:
                    self.off_topic = 0
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

    async def _new_task(self) -> None:
        """Clear per-task state before the caller's next request.

        The verified patient carries over only when the caller was the patient.
        Requests for someone else always verify again.
        """
        await self.close()
        if self.party.proxy or self.new_patient:
            known = None
        elif self.patient:
            known = self.patient if self.party.patient == "self" else None
        else:
            known = self.known_patient
        self.known_patient = known
        if not known:
            self.email_on_file, self.email_checked = None, False
        self.patient = None
        self.party = Party()
        self.stage = "intent"
        self.intent = "book"
        self.visit = "follow-up"
        self.day = self.week = self.period = None
        self.days = ()
        self.requested_time = self.after = self.before = None
        self.location = self.service = self.provider = None
        self.options, self.existing_options, self.shown_slots = [], [], set()
        self.single_existing = False
        self.old = None
        self.pending_email = self.staff_topic = None
        self.pending_request = None
        self.unclear = self.opening_interpreter_failures = self.off_topic = 0
        self.nameless_replies = 0
        self.safety_resume, self.safety_opening, self.safety_retries = None, None, 0
        self.name, self.dob, self.dob_fragment = "", None, None
        self.dob_year_start = ""
        self.dob_retries = self.identity_playback_retries = self.attempts = 0
        self.new_patient = None
        self.digits = ""
        self.last_operation = None
        self.task_work = False
