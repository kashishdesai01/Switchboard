"""The structured turn reading: its schema, the model input, and how the call uses it."""

import json

import httpx
from test_gaps import Clinic, run

from switchboard.intents import (
    GENERAL_ACTIONS,
    INFO_ACTIONS,
    NO_ACTION,
    ClaudeRouter,
    OllamaInterpreter,
    TurnMeaning,
    settle,
    turn_input,
    turn_schema,
)


def reading(**fields) -> TurnMeaning:
    base = {"answer": "not_applicable", "action": NO_ACTION, "value": "", "extra": ""}
    return TurnMeaning(**{**base, "needs_staff": False, "emergency": False, **fields})


def test_schema_requires_every_field_in_generation_order():
    schema = turn_schema({"name": "A name", NO_ACTION: "Only an answer", **GENERAL_ACTIONS})
    assert list(schema["properties"]) == [
        "doubt",
        "answer",
        "action",
        "value",
        "extra",
        "needs_staff",
        "emergency",
    ]
    assert schema["required"] == list(schema["properties"])
    assert schema["properties"]["answer"]["enum"] == ["yes", "no", "unclear", "not_applicable"]
    # On a yes/no question a bare answer comes first; general requests come last.
    assert schema["properties"]["action"]["enum"] == [
        NO_ACTION,
        "name",
        "unclear",
        *GENERAL_ACTIONS,
    ]


def test_none_is_offered_only_when_the_stage_lists_it():
    assert turn_schema({"name": "A name"})["properties"]["action"]["enum"] == ["name", "unclear"]


def test_old_readings_without_answer_or_extra_still_validate():
    meaning = TurnMeaning(action="book", value="", emergency=False, needs_staff=False)
    assert (meaning.answer, meaning.extra) == ("not_applicable", "")


def test_settle_maps_the_answer_onto_the_stage_vocabulary():
    assert settle(reading(answer="yes"), True).action == "affirm"
    assert settle(reading(answer="no"), True).action == "decline"
    assert settle(reading(answer="unclear"), True).action == "unclear"
    assert settle(reading(answer="yes", action="unclear"), True).action == "affirm"
    # A yes the model itself marked as doubtful or conditional is not consent.
    assert settle(reading(answer="yes", doubt=True), True).action == "unclear"
    assert settle(reading(answer="no", doubt=True), True).action == "decline"
    # Not a yes/no question: a reply with no stage action is unclear.
    assert settle(reading(answer="yes"), False).action == "unclear"
    # A stage action is kept whatever the answer.
    corrected = settle(reading(answer="no", action="name", value="Jane Smith"), True)
    assert (corrected.action, corrected.value) == ("name", "Jane Smith")


def test_settle_ignores_a_correction_that_repeats_the_question():
    question = "I have Maria Lopez, maria at example dot com. Is that correct?"
    echoed = settle(reading(answer="yes", action="name", value="Maria Lopez"), True, question)
    assert (echoed.action, echoed.value) == ("affirm", "")
    email = settle(reading(answer="yes", action="email", value="maria@example.com"), True, question)
    assert email.action == "affirm"
    changed = settle(reading(answer="no", action="name", value="Mia Lopez"), True, question)
    assert (changed.action, changed.value) == ("name", "Mia Lopez")
    # Outside a yes/no question a name is never an echo.
    assert settle(reading(action="name", value="Maria Lopez"), False, question).action == "name"


def test_model_input_ends_with_the_question_then_the_reply():
    context = {"stage": "check_name", "caller": "Yep.", "question": "Is that correct?"}
    text = turn_input(context, {"name": "A name", NO_ACTION: "Only an answer"})
    lines = text.splitlines()
    assert lines[0] == "Stage: check_name"
    assert lines[-2:] == ['The assistant asked: "Is that correct?"', 'The caller replied: "Yep."']
    assert [line.split(":")[0] for line in lines if line.startswith("- ")] == [
        "- none",
        "- name",
        "- unclear",
    ]


def test_ollama_sends_question_and_schema_and_returns_the_reading():
    sent = {}

    def handle(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        content = reading(answer="yes").model_dump_json()
        return httpx.Response(200, json={"message": {"content": content}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            model = OllamaInterpreter(client, "test", 1)
            context = {"stage": "proxy_permission", "question": "Do you have permission?"}
            meaning = await model.interpret_turn(
                {**context, "caller": "I do."}, {NO_ACTION: "Only an answer"}
            )
            assert meaning is not None and meaning.answer == "yes"
            user = sent["messages"][1]["content"]
            assert 'The assistant asked: "Do you have permission?"' in user
            assert sent["format"]["properties"]["action"]["enum"] == [NO_ACTION, "unclear"]
            # A reading with an action that wasn't offered is discarded.
            assert await model.interpret_turn({**context, "caller": "I do."}, {"x": "y"}) is None

    run(scenario())


def test_claude_router_fills_the_same_schema():
    sent = {}

    def handle(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        block = {"type": "tool_use", "name": "interpret_clinic_turn"}
        block["input"] = reading(answer="no").model_dump()
        return httpx.Response(200, json={"content": [block]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            router = ClaudeRouter(client, "key", "model")
            context = {"stage": "check_dob", "question": "Is that correct?", "caller": "Nope."}
            meaning = await router.interpret_turn(context, {NO_ACTION: "Only an answer"})
            assert meaning is not None and meaning.answer == "no"
            schema = sent["tools"][0]["input_schema"]
            assert schema["properties"]["action"]["enum"] == [NO_ACTION, "unclear"]
            assert 'asked: "Is that correct?"' in sent["messages"][0]["content"]

    run(scenario())


class Capture:
    def __init__(self, meaning: TurnMeaning):
        self.meaning = meaning
        self.seen: list[tuple[dict, dict]] = []

    async def interpret_turn(self, context, actions):
        self.seen.append((context, dict(actions)))
        return self.meaning


def test_call_gives_the_model_the_question_and_settles_the_answer(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        for text in ["book a follow-up", "For me.", "Maria Lopez", "July 2 1984"]:
            await session.say(text)
        assert session.call.stage == "check_dob"
        model = Capture(reading(answer="yes"))
        session.call.interpreter = model
        question = session.reply.text
        meaning = await session.call._interpret_turn("Yep, all good.")
        context, actions = model.seen[0]
        assert context["question"] == question[-300:]
        assert context["caller"] == "Yep, all good."
        # The question is not repeated in the recent turns.
        assert all(turn.get("assistant") != question[:400] for turn in context["recent"])
        assert NO_ACTION in actions and "affirm" not in actions and "decline" not in actions
        # On a yes/no question only the stage's own actions and the bare answer are offered.
        assert not set(GENERAL_ACTIONS) & set(actions)
        assert meaning is not None and meaning.action == "affirm"
        assert session.call._context_answer("yep all good", meaning) == "yes"

    run(scenario())


def test_openers_still_offer_general_requests_without_none(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        model = Capture(reading(action="unclear"))
        session.call.interpreter = model
        await session.call._interpret_turn("Hi.")
        _, actions = model.seen[0]
        assert set(GENERAL_ACTIONS) <= set(actions) and NO_ACTION not in actions

    run(scenario())


class Script:
    """Reads one reply as a clinic question and every other reply with the parsers."""

    def __init__(self, text: str, meaning: TurnMeaning):
        self.text, self.meaning = text, meaning
        self.seen: list[dict] = []

    async def interpret_turn(self, context, actions):
        self.seen.append(dict(actions))
        return self.meaning if context["caller"] == self.text else None


def test_clinic_question_mid_call_is_answered_and_the_question_repeated(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        for text in ["book a follow-up", "For me.", "Maria Lopez"]:
            await session.say(text)
        assert session.call.stage == "dob"
        question = session.reply.text
        session.call.interpreter = Script("What are your hours?", reading(action="hours"))
        await session.say("What are your hours?")
        assert session.call.stage == "dob"
        assert session.reply.text != question and session.reply.text.endswith(question)
        assert set(INFO_ACTIONS) <= set(session.call.interpreter.seen[0])
        assert "staff" not in session.call.interpreter.seen[0]

    run(scenario())


class Always:
    def __init__(self, meaning: TurnMeaning):
        self.meaning = meaning

    async def interpret_turn(self, context, actions):
        return self.meaning


def test_a_model_yes_commits_only_when_unqualified_and_undoubted(tmp_path):
    session = Clinic(tmp_path).session()
    call = session.call
    call.intent = "book"
    yes = reading(answer="yes", action="affirm")
    assert call._context_answer("yeah that works for me", yes, change=True) == "yes"
    # A bare okay is no consent to a change, even when the model reads it as yes.
    assert call._context_answer("okay", yes, change=True) is None
    assert call._context_answer("right", yes, change=True) is None
    # The regex cross-check vetoes a model yes on qualified words.
    assert call._context_answer("yes if it's after 3", yes, change=True) is None
    # A doubtful or unclear reading is never a yes, anywhere.
    doubtful = reading(answer="yes", action="affirm", doubt=True)
    assert call._context_answer("yes i think so", doubtful) is None
    assert call._context_answer("yes", reading(answer="unclear", action="unclear")) is None
    # Without a reading the parsers answer.
    assert call._context_answer("yes", None, change=True) == "yes"
    assert call._context_answer("okay", None, change=True) is None


def test_off_topic_staff_reading_reasks_in_context_before_handing_off(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        for text in ["book a follow-up", "For me."]:
            await session.say(text)
        assert session.call.stage == "name"
        session.call.interpreter = Always(reading(action="staff", needs_staff=True))
        await session.say("I have the permission.")
        assert session.call.stage == "name" and session.reply.outcome is None
        assert session.reply.text == "Thanks. What name should I use?"
        await session.say("I already told you I have permission.")
        assert session.reply.outcome == "handoff"

    run(scenario())


def test_an_answer_with_a_staff_flag_is_grounded(tmp_path):
    async def scenario():
        session = Clinic(tmp_path).session()
        for text in ["book a follow-up", "For me."]:
            await session.say(text)
        session.call.interpreter = Always(
            reading(action="name", value="Maria Lopez", needs_staff=True)
        )
        await session.say("Maria Lopez, and I'm running late")
        assert session.call.stage == "dob" and session.reply.outcome is None

    run(scenario())


def test_server_reads_turns_with_claude_when_both_models_are_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from switchboard.api import app

    monkeypatch.setenv("SWITCHBOARD_DB", str(tmp_path / "models.sqlite"))
    monkeypatch.setenv("SWITCHBOARD_CLAUDE_MODEL", "fixture-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-key")
    monkeypatch.setenv("SWITCHBOARD_OLLAMA_MODEL", "fixture-local")
    monkeypatch.setenv("SWITCHBOARD_OLLAMA_PORT", "9")
    with TestClient(app):
        assert isinstance(app.state.turn_interpreter, ClaudeRouter)
    monkeypatch.delenv("SWITCHBOARD_CLAUDE_MODEL")
    with TestClient(app):
        assert isinstance(app.state.turn_interpreter, OllamaInterpreter)


def test_claude_router_drops_a_reading_cut_off_by_max_tokens():
    def handle(request: httpx.Request) -> httpx.Response:
        block = {"type": "tool_use", "name": "interpret_clinic_turn"}
        block["input"] = reading(answer="no").model_dump()
        return httpx.Response(200, json={"content": [block], "stop_reason": "max_tokens"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            router = ClaudeRouter(client, "key", "model")
            context = {"stage": "check_dob", "question": "Is that correct?", "caller": "Nope."}
            assert await router.interpret_turn(context, {NO_ACTION: "Only an answer"}) is None

    run(scenario())


def test_patient_is_asked_only_where_a_request_can_open():
    assert "patient" in turn_schema({"book": "Book", "cancel": "Cancel"})["properties"]
    assert "patient" not in turn_schema({NO_ACTION: "Only an answer"})["properties"]
