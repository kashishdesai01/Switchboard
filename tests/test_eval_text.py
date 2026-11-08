import asyncio
import json

from evals.run_text import FIXED_BAYVIEW, SCENARIOS, Scenario, evaluate_case


def test_eval_rejects_unapproved_write_even_with_expected_end_state(tmp_path):
    case = next(
        item for item in json.loads(SCENARIOS.read_text()) if item["id"] == "book-follow-up"
    )
    case["turns"][-1] = "yes"
    result = asyncio.run(evaluate_case(tmp_path, FIXED_BAYVIEW, Scenario.model_validate(case)))

    assert result["outcome"] == "booked"
    assert result["failures"] == []
    assert result["safety_violations"] == ["appointment mutation outside the authorized turn"]
    assert not result["passed"]


def test_eval_detects_wrong_provider_with_correct_booking_count(tmp_path):
    case = next(
        item for item in json.loads(SCENARIOS.read_text()) if item["id"] == "book-follow-up"
    )
    case["appointments"][0]["provider"] = 1
    result = asyncio.run(evaluate_case(tmp_path, FIXED_BAYVIEW, Scenario.model_validate(case)))

    assert result["outcome"] == "booked"
    assert result["safety_violations"] == []
    assert result["failures"] == ["appointment state mismatch"]
    assert not result["passed"]
