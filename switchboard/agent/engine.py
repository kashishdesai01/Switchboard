"""AgentCall: Claude holds the conversation; code guards every read and write.

It has the interface transports use on the scripted ``Call`` (greeting, handle,
digit, close, trace, stage, playback_finished, interrupt, summary_outcome) and
returns the same ``Reply``. The guarantees live in the tool code below:

- No appointment data is read or changed until ``verify_patient`` matched the
  patient through ``Scheduling.identify`` (or drafted a new demo patient for a
  self booking when registration is allowed).
- Acting for someone else needs stated permission and is only allowed where the
  scripted call allows it (a front desk on the SQLite demo store).
- ``confirm_change`` commits only the proposal read back in an earlier turn, after
  the caller heard it in full and replied.
- The rules-based safety check runs on every caller turn before the model.
- Birth dates must be full YYYY-MM-DD dates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, time, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from switchboard.agent.tools import system_prompt, tool_definitions
from switchboard.call.confirmation import commit_change, confirmation_notice
from switchboard.call.core import Call
from switchboard.call.safety_routing import SafetyMixin
from switchboard.call.vocabulary import ANYTHING_ELSE, TASK_OUTCOMES, Party, Reply
from switchboard.frontdesk import email_address
from switchboard.operations import summary_outcome
from switchboard.safety import GREETING_NOTICE, Level, assess
from switchboard.scheduling import Change, Scheduling, instant
from switchboard.spoken import say_numbers
from switchboard.store import Conflict, DemoStore, Resource, Unavailable

if TYPE_CHECKING:
    from switchboard.frontdesk import FrontDesk
    from switchboard.operations import Trace

LOG = logging.getLogger("switchboard.agent")
MAX_TOOL_ROUNDS = 5
DEFAULT_MODEL = "claude-haiku-5-5"
APOLOGY = "Sorry, I didn't catch that. Could you say it again?"
UNAVAILABLE = "I'm sorry, I'm having trouble on my end. I'll pass you to the front desk."
NEEDS_REVIEW = (
    "I couldn't verify the outcome. Please check with the front desk before trying again."
)


def agent_model() -> str:
    return (
        os.getenv("SWITCHBOARD_AGENT_MODEL")
        or os.getenv("SWITCHBOARD_CLAUDE_MODEL")
        or DEFAULT_MODEL
    )


class ModelFailure(Exception):
    """The model or the network failed for this turn."""


class _Finish(Exception):
    """A tool ended the turn with a reply code wrote, without the model."""

    def __init__(self, reply: Reply):
        self.reply = reply


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


def _block(block: Any) -> dict | None:
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)}
    return None


def _spoken(text: str) -> str:
    return " ".join(re.sub(r"[*#`>]|^\s*[-•]\s*", "", text, flags=re.M).split())


class AgentCall:
    # Shared with the scripted call so times, calendars, staff work and safety
    # scripts are produced the same way.
    describe = Call.describe
    _calendar = Call._calendar
    _work = Call._work
    emergency_reply = SafetyMixin.emergency_reply
    urgent_reply = SafetyMixin.urgent_reply

    def __init__(
        self,
        scheduling: Scheduling,
        client: Any = None,
        trace: Trace | None = None,
        *,
        allow_registration: bool = False,
        frontdesk: FrontDesk | None = None,
        email_form: bool = False,
        voice: bool = False,
        model: str | None = None,
        timeout: float = 10.0,
    ):
        self.scheduling = scheduling
        self.client = client
        self.trace = trace
        self.allow_registration = allow_registration
        self.frontdesk = frontdesk
        self.email_form = email_form
        self.voice = voice
        self.model = model or agent_model()
        self.timeout = timeout
        self.proxy_allowed = bool(frontdesk and isinstance(scheduling.store, DemoStore))
        self.stage = "intent"
        self.messages: list[dict] = []
        self.greeting_text = ""
        self.turn = 0
        self.failures = 0
        self.digits = ""
        # Per task.
        self.party = Party()
        self.intent = "book"
        self.visit = "follow-up"
        self.patient: str | None = None
        self.new_patient: Resource | None = None
        self.email_on_file: str | None = None
        self.attempts = 0
        self.options: list[Resource] = []
        self.shown_slots: set[str] = set()
        self.appointments: list[Resource] = []
        self.old: Resource | None = None
        self.change: Change | None = None
        self.read_back = ""
        self.proposal_turn: int | None = None
        self.staff_topic: str | None = None
        self.last_operation: str | None = None
        self.task_work = False
        self.tasks = 0
        self.outcomes: list[str] = []
        # Per reply.
        self.prompt_id: str | None = None
        self.played = False
        self.last_reply: Reply | None = None
        self._turn_outcomes: list[str] = []
        self._turn_say: str | None = None
        self._turn_calendar: tuple[dict, ...] = ()
        self._turn_email_status: str | None = None

    # Transport interface

    def greeting(self) -> Reply:
        self.greeting_text = (
            f"Hi, thanks for calling {self.scheduling.clinic.name}. I'm the automated demo assistant. "
            f"{GREETING_NOTICE} How can I help today?"
        )
        return self.say(self.greeting_text)

    def summary_outcome(self) -> str | None:
        return summary_outcome(self.outcomes)

    def playback_finished(self, reply_id: str | None) -> None:
        if reply_id and reply_id == self.prompt_id:
            self.played = True

    def interrupt(self) -> None:
        self.prompt_id = None
        self.played = False

    async def close(self) -> None:
        change, self.change = self.change, None
        self.proposal_turn = None
        if change and change.hold:
            try:
                await self.scheduling.release(change.hold)
            except Unavailable:
                # The authoritative FHIR hold contains its own expiry.
                pass

    async def digit(self, digit: str) -> Reply | None:
        if re.fullmatch(r"[0-9]", digit):
            self.digits += digit
        if self.digits and (len(self.digits) >= 8 or digit == "#"):
            keyed, self.digits = self.digits, ""
            return await self.handle(f"User keyed: {keyed}", record="[Digits entered on keypad]")
        return None

    async def handle(self, text: str, *, record: str | None = None) -> Reply:
        stage = self.stage
        if self.trace:
            self.trace.record("caller", record or text)
        started = monotonic()
        try:
            return await self._handle_turn(text.strip())
        finally:
            if self.trace:
                self.trace.add(
                    "turn_processed",
                    state=f"{stage} → {self.stage}",
                    latency_ms=round((monotonic() - started) * 1000),
                )

    def say(
        self,
        text: str,
        *,
        outcome: str | None = None,
        confirm: bool = False,
        calendar: tuple[dict, ...] = (),
        email_status: str | None = None,
        recorded: bool = False,
    ) -> Reply:
        ends_call = bool(outcome) and outcome not in TASK_OUTCOMES
        if outcome in TASK_OUTCOMES and not text.rstrip().endswith("?"):
            text += ANYTHING_ELSE
        # Text-to-speech reads "9:30" as "nine three zero": write numbers as words.
        text = say_numbers(text)
        reply = Reply(text, uuid4().hex, outcome, confirm, calendar, email_status, ends_call)
        self.last_reply = reply
        if self.trace:
            self.trace.record("assistant", text)
        self.prompt_id, self.played = (reply.id if confirm else None), False
        if outcome and not recorded:
            self._record_outcome(outcome)
        return reply

    # The turn

    async def _handle_turn(self, text: str) -> Reply:
        rules, _ambiguous = assess(text)
        if rules.level.acute:
            # The model never sees this turn.
            await self.close()
            return self.emergency_reply(result=rules)
        if self.stage == "closed":
            return self.say("This call has ended.", outcome="closed")
        if rules.level is Level.URGENT:
            return await self.urgent_reply(rules)
        self.turn += 1
        self._add_caller(text)
        self._turn_outcomes, self._turn_say = [], None
        self._turn_calendar, self._turn_email_status = (), None
        try:
            spoken = _spoken(await self._converse())
            self.failures = 0
        except _Finish as finish:
            return finish.reply
        except ModelFailure:
            if self._turn_say is None:
                self.failures += 1
                if self.failures >= 2:
                    await self.close()
                    self.staff_topic = "assistant unavailable"
                    self._work("staff_requested")
                    return self.say(UNAVAILABLE, outcome="handoff")
                return self.say(APOLOGY)
            spoken = ""
        confirm = False
        if self.change:
            # While a change waits for a yes, every reply ends with its read-back word
            # for word, so the caller's next yes is to exactly what will be written.
            if self.read_back not in spoken:
                spoken = (
                    self.read_back
                    if self.proposal_turn == self.turn or not spoken
                    else f"{spoken} {self.read_back}"
                )
            confirm = True
            self._turn_calendar = (
                self._calendar([self.change.hold.slot], "held") if (self.change.hold) else ()
            )
        spoken = spoken or self._turn_say or "Sorry, could you say that again?"
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages[-1]["content"] = [{"type": "text", "text": spoken}]
        else:
            self.messages.append(
                {"role": "assistant", "content": [{"type": "text", "text": spoken}]}
            )
        outcomes = self._turn_outcomes
        outcome = next(
            (item for item in reversed(outcomes) if item not in TASK_OUTCOMES),
            outcomes[-1] if outcomes else None,
        )
        return self.say(
            spoken,
            outcome=outcome,
            confirm=confirm,
            calendar=self._turn_calendar,
            email_status=self._turn_email_status,
            recorded=True,
        )

    def _add_caller(self, text: str) -> None:
        block = {"type": "text", "text": text}
        # After a failed turn the caller's words wait in the last user message.
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages[-1]["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    async def _create(self, **params: Any) -> Any:
        try:
            if self.client is None:
                import anthropic

                self.client = anthropic.AsyncAnthropic(max_retries=1, timeout=self.timeout)
            async with asyncio.timeout(self.timeout):
                return await self.client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001 - any model or network failure
            LOG.warning("Agent model call failed: %s", type(exc).__name__)
            raise ModelFailure from exc

    async def _converse(self) -> str:
        clinic = self.scheduling.clinic
        system = system_prompt(clinic, self.scheduling.now(), self.greeting_text)
        tools = tool_definitions(clinic)
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": 400,
                "system": system,
                "tools": tools,
                "messages": self.messages,
            }
            if round_ == MAX_TOOL_ROUNDS:
                params["tool_choice"] = {"type": "none"}
            response = await self._create(**params)
            content = [item for item in map(_block, response.content) if item]
            uses = [item for item in content if item["type"] == "tool_use"]
            if round_ == MAX_TOOL_ROUNDS or not uses:
                text = " ".join(item["text"] for item in content if item["type"] == "text")
                self.messages.append(
                    {"role": "assistant", "content": [{"type": "text", "text": text}]}
                )
                return text
            self.messages.append({"role": "assistant", "content": content})
            results = []
            for use in uses:
                result = await self._run_tool(use["name"], use["input"])
                entry = {
                    "type": "tool_result",
                    "tool_use_id": use["id"],
                    "content": json.dumps(result),
                }
                if result.get("ok") is False:
                    entry["is_error"] = True
                results.append(entry)
            self.messages.append({"role": "user", "content": results})
        raise AssertionError("unreachable")

    async def _run_tool(self, name: str, args: dict) -> dict:
        if self.stage == "closed" or any(o not in TASK_OUTCOMES for o in self._turn_outcomes):
            return _error("The call is ending. Say a short goodbye.")
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return _error(f"Unknown tool {name}.")
        if self.trace:
            self.trace.add("agent_tool", state=name)
        try:
            return await handler(args if isinstance(args, dict) else {})
        except Conflict:
            await self.close()
            return _error(
                "That time or appointment just changed. Search again and offer new times."
            )
        except Unavailable:
            await self.close()
            raise _Finish(self.say(NEEDS_REVIEW, outcome="needs_review")) from None

    # Outcomes and task state

    def _record_outcome(self, outcome: str) -> None:
        """Record an outcome the way the scripted call's say() does."""
        self._turn_outcomes.append(outcome)
        if outcome not in {"goodbye", "closed"}:
            self.outcomes.append(outcome)
            if self.trace:
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
        if outcome not in TASK_OUTCOMES:
            self.stage = "closed"
            return
        self.stage = "anything_else"
        self.tasks += 1
        self.task_work = False
        # The verified patient carries over only when the caller was the patient.
        if self.party.proxy or self.new_patient:
            self.patient = self.new_patient = None
            self.email_on_file = None
            self.party = Party()
        self.options, self.appointments, self.shown_slots = [], [], set()
        self.old = None
        self.staff_topic = self.last_operation = None

    def _preferences(self) -> dict[str, str]:
        return {"visit": self.visit}

    def _patient_error(self) -> dict | None:
        if self.patient is None:
            return _error(
                "The patient is not verified. Get their full name and full birth date and call "
                "verify_patient first."
            )
        return None

    def _propose(self, change: Change) -> dict:
        self.change = change
        self.proposal_turn = self.turn
        self.interrupt()
        whose = "the patient's" if self.party.proxy else "your"
        if change.action == "cancel":
            assert change.old is not None
            text = f"Cancel {whose} appointment on {self.describe(change.old)}?"
        else:
            assert change.hold is not None
            slot = self.describe(change.hold.slot)
            if change.action == "reschedule":
                assert change.old is not None
                text = f"Move {whose} appointment from {self.describe(change.old)} to {slot}?"
            elif change.new_patient is not None:
                # The name and birth date are written too, so the caller hears them.
                born = date.fromisoformat(change.new_patient["birthDate"])
                text = (
                    f"Create a new demo patient record for {change.new_patient['name'][0]['text']}, "
                    f"born {born:%B} {born.day}, {born.year}, and book {whose} {self.visit} "
                    f"on {slot}?"
                )
            else:
                text = f"Book {whose} {self.visit} on {slot}?"
        self.read_back = text + " Please say yes or no."
        self._turn_say = self.read_back
        self.stage = "confirm_change"
        if self.trace:
            self.trace.add("change_proposed", state=change.action)
        return {
            "ok": True,
            "read_back": self.read_back,
            "next": "Say the read_back exactly and wait for the caller's answer.",
        }

    def _pick(self, items: list[Resource], number: Any, kind: str) -> tuple[Resource | None, dict]:
        """The numbered item, or None and an error for the model."""
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= len(items):
            return items[number - 1], {}
        source = "find_times" if kind == "option" else "list_appointments"
        return None, _error(f"No {kind} {number}. Use a number from the latest {source} result.")

    # Tools

    async def _tool_verify_patient(self, args: dict) -> dict:
        name = " ".join(str(args.get("full_name", "")).split())
        raw = str(args.get("birth_date", ""))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return _error(
                "birth_date must be the full date as YYYY-MM-DD. Ask the caller for the missing "
                "day, month or year."
            )
        try:
            dob = date.fromisoformat(raw)
        except ValueError:
            return _error("That birth date is not a real date. Ask the caller again.")
        today = self.scheduling.now().astimezone(self.scheduling.clinic.timezone).date()
        if dob > today or dob.year < today.year - 130:
            return _error("That birth date can't be right. Ask the caller again.")
        if len(name.split()) < 2:
            return _error("Ask for the patient's first and last name.")
        for_self = args.get("for_self")
        if not isinstance(for_self, bool):
            return _error("Say whether the caller is the patient (for_self).")
        request = (
            args.get("request")
            if args.get("request") in {"book", "reschedule", "cancel"}
            else "book"
        )
        if not for_self:
            if not self.proxy_allowed:
                self.intent = request
                self.party = Party("other")
                self.staff_topic = "proxy request"
                return _error(
                    "This line can't handle requests for someone else. Tell the caller the front "
                    "desk will help, then call transfer_to_staff."
                )
            if args.get("permission_stated") is not True:
                return _error(
                    "Ask whether the caller has the patient's permission. Call again with "
                    "permission_stated true only if they say they do."
                )
        await self.close()
        self.patient = self.new_patient = self.email_on_file = None
        self.options, self.appointments, self.shown_slots, self.old = [], [], set(), None
        self.intent = request
        patient = await self.scheduling.identify(name, dob)
        if patient is None and self.allow_registration and request == "book":
            self.new_patient = await self.scheduling.draft_demo_patient(name, dob)
            if self.new_patient is not None:
                patient = self.new_patient["id"]
                self.visit = "first visit"
        if patient is None:
            self.attempts += 1
            self.party = Party()
            if self.attempts >= 3:
                return _error(
                    "Still no match. Tell the caller the front desk can help, then call "
                    "transfer_to_staff."
                )
            return _error(
                "No single matching record. Ask the caller to check the name spelling and birth "
                "date. Don't say whether any record exists."
            )
        self.attempts = 0
        self.patient = patient
        self.party = Party("self", None, None) if for_self else Party("other", None, name)
        if self.frontdesk and for_self and not self.new_patient:
            stored = await self.scheduling.email_on_file(patient)
            self.email_on_file = email_address(stored) if stored else None
        if self.trace:
            self.trace.add("patient_verified", state="new" if self.new_patient else "found")
        return {
            "ok": True,
            "verified": True,
            "new_patient": self.new_patient is not None,
            **(
                {
                    "note": "No record was found. A new patient record will be created when "
                    "they book. Don't say they are verified; say you'll set them up as a new "
                    "patient."
                }
                if self.new_patient is not None
                else {}
            ),
            "patient": "self" if for_self else "other",
        }

    async def _tool_find_times(self, args: dict) -> dict:
        if error := self._patient_error():
            return error
        clinic = self.scheduling.clinic
        visit = "first visit" if self.new_patient else args.get("visit_type", self.visit)
        if visit not in {"follow-up", "first visit"}:
            return _error("visit_type must be follow-up or first visit.")
        try:
            day = date.fromisoformat(args["day"]) if args.get("day") else None
            clocks = {
                key: time.fromisoformat(args[key]) if args.get(key) else None
                for key in ("time", "after", "before")
            }
        except (TypeError, ValueError):
            return _error("Use YYYY-MM-DD for day and HH:MM for times.")
        provider = args.get("provider") or None
        if provider is not None and provider not in clinic.providers:
            return _error(f"Unknown provider. Use one of {sorted(clinic.providers)}.")
        location = args.get("location") or None
        if location is not None and location not in clinic.locations:
            return _error(f"Unknown location. Use one of {sorted(clinic.locations)}.")
        period = args.get("period") if args.get("period") in {"morning", "afternoon"} else None
        if args.get("appointment") is not None:
            old, error = self._pick(self.appointments, args["appointment"], "appointment")
            if old is None:
                return error
            self.old = old
            if not self.new_patient:
                duration = instant(old["end"]) - instant(old["start"])
                visit = "first visit" if duration == timedelta(minutes=40) else "follow-up"
        if not args.get("more"):
            self.shown_slots = set()
        self.visit = visit
        self.options = await self.scheduling.options(
            visit,
            day,
            provider,
            period,
            None,
            clocks["time"],
            location=location,
            after=clocks["after"],
            before=clocks["before"],
            exclude=set(self.shown_slots),
            limit=2 if self.voice else 3,
            patient=None if self.new_patient else self.patient,
            ignore=self.old["id"] if self.old else None,
        )
        self.shown_slots.update(slot["id"] for slot in self.options)
        if not self.options:
            return {
                "ok": True,
                "options": [],
                "note": "No openings match. Ask about another day, time, provider or location.",
            }
        return {
            "ok": True,
            "options": [
                {"option": index, "time": self.describe(slot)}
                for index, slot in enumerate(self.options, 1)
            ],
        }

    async def _tool_list_appointments(self, args: dict) -> dict:
        if error := self._patient_error():
            return error
        assert self.patient is not None
        self.appointments = (
            [] if self.new_patient else await self.scheduling.appointments(self.patient, limit=None)
        )
        return {
            "ok": True,
            "appointments": [
                {
                    "appointment": index,
                    "time": self.describe(item),
                    "can_change": self.scheduling.can_change(item),
                }
                for index, item in enumerate(self.appointments, 1)
            ],
        }

    async def _tool_propose_booking(self, args: dict) -> dict:
        if error := self._patient_error():
            return error
        slot, error = self._pick(self.options, args.get("option"), "option")
        if slot is None:
            return error
        await self.close()
        self.intent, self.old = "book", None
        hold = await self.scheduling.hold(slot)
        assert self.patient is not None
        return self._propose(
            Change(hold.operation, "book", self.patient, None, hold, self.new_patient)
        )

    def _changeable(self, number: Any) -> tuple[Resource | None, dict]:
        if self.new_patient:
            return None, _error("A new patient has no appointments to change.")
        old, error = self._pick(self.appointments, number, "appointment")
        if old is None:
            return None, error
        if not self.scheduling.can_change(old):
            return None, _error(
                "This appointment is too close to change here. Tell the caller the front desk can "
                "help and offer transfer_to_staff."
            )
        return old, {}

    async def _tool_propose_reschedule(self, args: dict) -> dict:
        if error := self._patient_error():
            return error
        old, error = self._changeable(args.get("appointment"))
        if old is None:
            return error
        slot, error = self._pick(self.options, args.get("option"), "option")
        if slot is None:
            return error
        await self.close()
        self.intent, self.old = "reschedule", old
        hold = await self.scheduling.hold(slot)
        assert self.patient is not None
        return self._propose(Change(hold.operation, "reschedule", self.patient, old, hold))

    async def _tool_propose_cancel(self, args: dict) -> dict:
        if error := self._patient_error():
            return error
        old, error = self._changeable(args.get("appointment"))
        if old is None:
            return error
        await self.close()
        self.intent, self.old = "cancel", old
        assert self.patient is not None
        return self._propose(Change(uuid4().hex, "cancel", self.patient, old, None))

    async def _tool_confirm_change(self, args: dict) -> dict:
        change = self.change
        if change is None:
            return _error("Nothing is proposed. Use a propose tool first.")
        if self.patient is None or change.patient != self.patient:
            return _error("The patient is not verified. Nothing was changed.")
        if self.proposal_turn is None or self.proposal_turn >= self.turn:
            return _error(
                "The caller hasn't answered the read-back yet. Say the read_back and wait for "
                "their reply. Nothing was changed."
            )
        if not self.played:
            # Interrupted or unheard: read it again this turn.
            self.proposal_turn = self.turn
            return _error(
                "The caller didn't hear the whole read-back. Say the read_back again and wait. "
                "Nothing was changed."
            )
        self.interrupt()
        self.last_operation = change.operation
        desk = self.frontdesk if self.frontdesk and not self.party.proxy else None
        await commit_change(
            self.scheduling,
            change,
            trace=self.trace,
            desk=desk,
            describe=self.describe,
            email=self.email_on_file,
        )
        self.change, self.proposal_turn = None, None
        outcome = {"book": "booked", "reschedule": "rescheduled", "cancel": "cancelled"}[
            change.action
        ]
        if change.hold:
            note = ""
            if desk:
                delivery, note = await confirmation_notice(desk, change.operation)
                self._turn_email_status = delivery if self.email_form else None
            when = self.describe(change.hold.slot)
            text = (
                f"The patient is {outcome} for {when}."
                if self.party.proxy
                else f"You're {outcome} for {when}."
            ) + note
            self._turn_calendar = self._calendar([change.hold.slot], "booked")
        else:
            text = (
                "The patient's appointment was cancelled."
                if self.party.proxy
                else "Your appointment was cancelled."
            )
        self._turn_say = text
        self._record_outcome(outcome)
        return {"ok": True, "result": outcome, "say": text}

    async def _tool_decline_change(self, args: dict) -> dict:
        if self.change is None:
            return _error("Nothing is proposed.")
        await self.close()
        self._turn_say = "Okay, I made no changes."
        self._record_outcome("declined")
        return {"ok": True, "result": "declined", "say": self._turn_say}

    async def _tool_transfer_to_staff(self, args: dict) -> dict:
        reason = " ".join(str(args.get("reason", "")).split())[:80]
        if self.staff_topic != "proxy request":
            self.staff_topic = reason or "staff requested"
        await self.close()
        self._work("staff_requested")
        self._turn_say = (
            "Of course. I'll pass this request to the front desk."
            if self.frontdesk
            else "This request needs the front desk."
        )
        self._record_outcome("handoff")
        return {"ok": True, "result": "handoff", "say": self._turn_say}

    async def _tool_report_emergency(self, args: dict) -> dict:
        await self.close()
        raise _Finish(self.emergency_reply())

    async def _tool_end_call(self, args: dict) -> dict:
        await self.close()
        self._turn_say = "Thanks for calling. Goodbye."
        self._record_outcome("goodbye")
        return {"ok": True, "say": self._turn_say}
