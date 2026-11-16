"""The call obeys a correct reading of every reply in the phrasing corpus.

Each case in evals/phrasings.toml is driven through a real Call. A fake interpreter
returns the corpus's expected reading for each turn (or, for reported live-call
failures, the reading the real model gave), so these tests check the code, not
the model. evals/run_phrasings.py checks the model against the same corpus.
"""

import pytest
from test_gaps import Clinic, require, run

from evals.phrasings import Case, Turn, behavior_failures, load, play
from switchboard.call.vocabulary import CONFIRMATION_STAGES
from switchboard.intents import NO_ACTION, TurnMeaning

CORPUS = load()


class CorpusInterpreter:
    """Returns the reading the harness sets before each turn."""

    def __init__(self):
        self.turn: Turn | None = None

    def prepare(self, turn: Turn) -> None:
        self.turn = turn

    async def interpret_turn(self, context, actions) -> TurnMeaning | None:
        assert self.turn is not None
        reading = self.turn.live_reading or self.turn.reading
        return reading.meaning(context["caller"])


def marked(case: Case):
    if case.xfail:
        # Only a behavior assertion counts as the known failure; a broken setup errors.
        return pytest.param(
            case,
            id=case.id,
            marks=pytest.mark.xfail(reason=case.xfail, strict=True, raises=AssertionError),
        )
    return pytest.param(case, id=case.id)


@pytest.mark.parametrize("case", [marked(case) for case in CORPUS.cases])
def test_call_obeys_correct_reading(case: Case, tmp_path):
    async def scenario():
        session = Clinic(tmp_path, existing=CORPUS.setups[case.setup].existing).session()
        model = CorpusInterpreter()
        records = await play(case, CORPUS.setups, session, model, before_turn=model.prepare)
        for record in records:
            # A live reading is what a model returned on a past call, possibly under an
            # older action vocabulary; only the correct reading must be offered now.
            reading = record.turn.reading
            require(record.actions is not None, f"{case.id}: no model call for {record.turn.say!r}")
            offered = {*record.actions, "unclear"}
            if record.stage in CONFIRMATION_STAGES:
                offered.add(NO_ACTION)
            require(
                set(reading.model_actions) <= offered,
                f"{case.id}: reading {reading.actions} not offered at {record.stage}: {sorted(offered)}",
            )
        problems = behavior_failures(case, session, records)
        assert not problems, problems

    run(scenario())


REQUIRED_STAGES = {
    "intent",
    "booking_recipient",
    "proxy_permission",
    "name",
    "check_name",
    "dob",
    "check_dob",
    "other_provider",
    "day_alternative",
    "select_slot",
    "confirm_change",
    "email_readback",
    "anything_else",
}


def test_corpus_covers_every_question_stage():
    counts = {stage: 0 for stage in REQUIRED_STAGES}
    for case in CORPUS.cases:
        counts[case.stage] = counts.get(case.stage, 0) + 1
    assert all(counts[stage] >= 8 for stage in REQUIRED_STAGES), counts
    reported = {case.reported for case in CORPUS.cases if case.reported}
    assert reported == {1, 2, 3, 4}
    assert all(case.must_pass for case in CORPUS.cases if case.reported)


@pytest.mark.parametrize("model_action", [None, "book", "unclear"])
@pytest.mark.parametrize("greeting", ["Hi.", "Hello, how are you?", "Good morning!"])
def test_a_bare_greeting_is_answered_with_how_can_i_help(greeting, model_action, tmp_path):
    """With no model, or one that reads a greeting as a request, the call asks how to help."""

    class Fixed:
        async def interpret_turn(self, context, actions):
            return TurnMeaning(action=model_action, value="", needs_staff=False, emergency=False)

    async def scenario():
        session = Clinic(tmp_path).session()
        if model_action:
            session.call.interpreter = Fixed()
        await session.say(greeting)
        assert session.call.stage == "intent"
        assert "How can I help" in session.reply.text
        assert "for you or for someone else" not in session.reply.text

    run(scenario())
