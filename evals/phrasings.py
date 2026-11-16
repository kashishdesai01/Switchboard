"""The phrasing corpus: caller replies in their own words and what they should mean.

``evals/phrasings.toml`` lists, for each stage that asks a question, differently
worded replies with the reading a correct model gives them and what the call
should then do. Two harnesses share this module:

* ``tests/test_phrasings.py`` gives the call a fake interpreter that returns the
  expected reading, and checks that the code obeys it.
* ``evals/run_phrasings.py`` gives the call the real interpreter, and checks that
  the model produces the expected reading.

A case starts from a named setup: caller turns, answered without a model, that
bring a fresh call to the stage. The reply to the last setup turn is the question
the case answers. Readings use the call's action vocabulary (``TURN_ACTIONS``
plus affirm/decline on confirmation stages). The model itself answers a yes/no
question in its ``answer`` field and uses the ``none`` action for a bare answer;
``settle`` maps that onto affirm/decline, so "affirm" in a reading means answer
yes and no stage action. On confirmation stages the expected answer follows from
the action (affirm: yes, decline: no, unclear: unclear) unless the reading states
``answer``.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from switchboard.call.vocabulary import CONFIRMATION_STAGES
from switchboard.intents import NO_ACTION, TurnMeaning, settle
from switchboard.safety import Level
from switchboard.spoken import parse_date

CORPUS = Path(__file__).with_name("phrasings.toml")
# In a reading's value, the caller's exact reply (the "self" action must quote it).
REPLY = "<reply>"

Answer = Literal["yes", "no", "unclear", "not_applicable"]
# Corpus actions that are only a yes/no answer, and the answer each action implies.
BARE_ANSWERS = {"affirm", "decline"}
IMPLIED_ANSWERS = {"affirm": "yes", "decline": "no", "unclear": "unclear"}

Expect = Literal["advance", "reask", "decline", "handoff", "booked", "goodbye", "info", "no_write"]


class Reading(BaseModel):
    """What a correct model reads in one caller turn."""

    model_config = ConfigDict(extra="forbid")
    # One action, or several that are equally correct (the first is used by the fake).
    action: str | list[str]
    value: str | None = None
    value_contains: str | None = None
    emergency: bool = False
    needs_staff: bool | None = None
    # Who an opening request is for (self / other); unscored when None.
    patient: Literal["self", "other"] | None = None
    # The answer to the question just asked (yes / no / unclear / not_applicable), or
    # several equally correct ones. Defaults to the one the action implies.
    answer: Answer | list[Answer] | None = None

    @property
    def actions(self) -> list[str]:
        return [self.action] if isinstance(self.action, str) else self.action

    @property
    def model_actions(self) -> list[str]:
        """The actions as the model names them: a bare yes or no is the none action."""
        return [NO_ACTION if a in BARE_ANSWERS else a for a in self.actions]

    def answers(self, stage: str) -> list[str] | None:
        """The acceptable answers to the question, or None when the answer isn't scored."""
        if self.answer is not None:
            return [self.answer] if isinstance(self.answer, str) else self.answer
        if stage not in CONFIRMATION_STAGES:
            return None
        implied = {IMPLIED_ANSWERS.get(action) for action in self.actions}
        return [implied.pop()] if len(implied) == 1 and None not in implied else None

    def meaning(self, said: str) -> TurnMeaning:
        """The reading as the model returns it (before ``settle``), for the fake interpreter."""
        value = self.value if self.value is not None else self.value_contains or ""
        action = self.actions[0]
        answers = [self.answer] if isinstance(self.answer, str) else self.answer
        return TurnMeaning(
            answer=answers[0] if answers else IMPLIED_ANSWERS.get(action, "not_applicable"),
            action=NO_ACTION if action in BARE_ANSWERS else action,
            value=said.strip() if value == REPLY else value,
            needs_staff=bool(self.needs_staff),
            emergency=self.emergency,
            patient=self.patient or "unknown",
        )


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: str
    reading: Reading
    # The reading a real model gave on a reported live call, when it differs from the
    # correct one. Only the fake uses it: the code must still do the right thing.
    live_reading: Reading | None = None


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    question: str
    turns: list[str] = Field(default_factory=list)
    existing: int = 0


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    stage: str
    setup: str | None = None
    question: str = ""
    say: str | None = None
    reading: Reading | None = None
    live_reading: Reading | None = None
    turns: list[Turn] = Field(default_factory=list)
    expect: Expect
    to: str | None = None
    reply_has: str | None = None
    reply_lacks: str | None = None
    state: dict[str, str] = Field(default_factory=dict)
    must_pass: bool
    reported: int | None = None
    xfail: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def one_form(self) -> Case:
        single = self.say is not None or self.reading is not None
        if single == bool(self.turns):
            raise ValueError(f"{self.id}: give either say and reading, or turns")
        if single:
            if self.say is None or self.reading is None:
                raise ValueError(f"{self.id}: say and reading go together")
            self.turns = [Turn(say=self.say, reading=self.reading, live_reading=self.live_reading)]
        elif self.live_reading is not None:
            raise ValueError(f"{self.id}: put live_reading on the turn")
        if (self.expect == "advance") != (self.to is not None):
            raise ValueError(f"{self.id}: advance needs to, and only advance takes it")
        return self


class Corpus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setups: dict[str, Setup]
    cases: list[Case]

    @model_validator(mode="after")
    def resolve(self) -> Corpus:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        for case in self.cases:
            case.setup = case.setup or case.stage
            setup = self.setups.get(case.setup)
            if setup is None:
                raise ValueError(f"{case.id}: no setup named {case.setup}")
            if setup.stage != case.stage:
                raise ValueError(f"{case.id}: setup {case.setup} reaches {setup.stage}")
            case.question = case.question or setup.question
        return self


def load(path: Path = CORPUS) -> Corpus:
    return Corpus.model_validate(tomllib.loads(path.read_text()))


@dataclass
class TurnRecord:
    """One caller turn of a case and what the call made of it."""

    turn: Turn
    stage: str
    question: str
    reply_text: str
    outcome: str | None
    actions: dict[str, str] | None = None
    meaning: TurnMeaning | None = None
    seconds: float | None = None
    safety: Level | None = None


@dataclass
class Recorder:
    """Wraps an interpreter and records what it was offered and what it returned."""

    inner: object
    actions: dict[str, str] | None = None
    meaning: TurnMeaning | None = None
    seconds: float | None = None
    safety: Level | None = None
    calls: int = field(default=0)

    def reset(self) -> None:
        self.actions = self.meaning = self.seconds = self.safety = None
        self.calls = 0

    async def interpret_turn(self, context: dict, actions: dict[str, str]) -> TurnMeaning | None:
        began = perf_counter()
        meaning = await self.inner.interpret_turn(context, actions)
        # A turn that resumes after a safety question can call the interpreter twice.
        # The first call reads the caller's own reply.
        if self.calls == 0:
            self.actions, self.meaning = dict(actions), meaning
            self.seconds = perf_counter() - began
        self.calls += 1
        return meaning

    async def assess_safety(self, text: str, recent: list[dict[str, str]]) -> Level | None:
        assessor = getattr(self.inner, "assess_safety", None)
        if assessor is None:
            return None
        level = await assessor(text, recent)
        if self.safety is None:
            self.safety = level
        return level


class SetupError(RuntimeError):
    """The setup did not bring the call to the case's stage and question."""


async def play(
    case: Case,
    setups: dict[str, Setup],
    session,
    interpreter,
    before_turn: Callable[[Turn], None] | None = None,
) -> list[TurnRecord]:
    """Bring a fresh session to the case's stage, then say the case's turns.

    ``session`` is a tests.test_gaps.Session. The setup runs without a model so it
    reaches the stage the same way every time; the case turns use ``interpreter``.
    """
    setup = setups[case.setup]
    session.call.interpreter = None
    for text in setup.turns:
        await session.say(text)
    if session.call.stage != case.stage or case.question not in session.reply.text:
        raise SetupError(
            f"{case.id}: setup {case.setup} reached {session.call.stage!r} "
            f"with {session.reply.text!r}"
        )
    recorder = Recorder(interpreter)
    session.call.interpreter = recorder
    records = []
    for turn in case.turns:
        if before_turn:
            before_turn(turn)
        recorder.reset()
        stage, question = session.call.stage, session.reply.text
        reply = await session.say(turn.say)
        records.append(
            TurnRecord(
                turn,
                stage,
                question,
                reply.text,
                reply.outcome,
                recorder.actions,
                recorder.meaning,
                recorder.seconds,
                recorder.safety,
            )
        )
    return records


def _resolve(state, dotted: str) -> str:
    value = state
    for part in dotted.split("."):
        value = getattr(value, part)
    return str(value)


def behavior_failures(case: Case, session, records: list[TurnRecord]) -> list[str]:
    """How the call's handling of the case differs from what the corpus expects."""
    last = records[-1]
    stage, outcome = session.call.stage, last.outcome
    problems = []
    wanted = {
        "advance": (None, case.to),
        "reask": (None, case.stage),
        "no_write": (None, None),
        "decline": ("declined", None),
        "handoff": ("handoff", None),
        "booked": ("booked", None),
        "goodbye": ("goodbye", None),
        "info": ("info_only", None),
    }[case.expect]
    if outcome != wanted[0]:
        problems.append(f"outcome {outcome!r}, expected {wanted[0]!r}")
    if wanted[1] is not None and stage != wanted[1]:
        problems.append(f"stage {stage!r}, expected {wanted[1]!r}")
    if case.reply_has and case.reply_has.casefold() not in last.reply_text.casefold():
        problems.append(f"reply lacks {case.reply_has!r}")
    if case.reply_lacks and case.reply_lacks.casefold() in last.reply_text.casefold():
        problems.append(f"reply has {case.reply_lacks!r}")
    for name, expected in case.state.items():
        actual = _resolve(session.call, name)
        if actual != expected:
            problems.append(f"{name} is {actual!r}, expected {expected!r}")
    if problems:
        problems.append(f"reply: {last.reply_text!r}")
    return problems


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w@.' ]", " ", text.casefold()).replace("dr.", "dr").split())


def _same_value(expected: str, actual: str) -> bool:
    if _plain(expected) == _plain(actual):
        return True
    day = parse_date(expected)
    return day is not None and day == parse_date(actual)


def reading_failures(record: TurnRecord) -> list[str]:
    """How the interpreter's reading of a turn differs from the expected one."""
    expected = record.turn.reading
    meaning = record.meaning
    if meaning is None:
        return ["no reading (model unavailable, timed out, or invalid)"]
    problems = []
    if answer_ok(record) is False:
        problems.append(
            f"answer {meaning.answer!r}, expected {'/'.join(expected.answers(record.stage))}"
        )
    meaning = settle(meaning, record.stage in CONFIRMATION_STAGES)
    if meaning.action not in expected.actions:
        problems.append(f"action {meaning.action!r}, expected {'/'.join(expected.actions)}")
    elif expected.value is not None:
        wanted = record.turn.say.strip() if expected.value == REPLY else expected.value
        if not _same_value(wanted, meaning.value):
            problems.append(f"value {meaning.value!r}, expected {wanted!r}")
    elif expected.value_contains and _plain(expected.value_contains) not in _plain(meaning.value):
        problems.append(f"value {meaning.value!r} lacks {expected.value_contains!r}")
    if expected.patient and meaning.patient != expected.patient:
        problems.append(f"patient {meaning.patient!r}, expected {expected.patient!r}")
    if meaning.emergency != expected.emergency:
        problems.append(f"emergency={meaning.emergency}")
    if expected.needs_staff is not None and meaning.needs_staff != expected.needs_staff:
        problems.append(f"needs_staff={meaning.needs_staff}")
    if not expected.emergency and record.safety not in {None, Level.NONE}:
        problems.append(f"safety check raised {record.safety.name}")
    return problems


def answer_ok(record: TurnRecord) -> bool | None:
    """Whether the model answered the yes/no question as expected; None if not scored."""
    wanted = record.turn.reading.answers(record.stage)
    if wanted is None:
        return None
    return record.meaning is not None and record.meaning.answer in wanted
