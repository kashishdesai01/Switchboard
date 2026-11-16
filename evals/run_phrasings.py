"""Check a real turn interpreter against the phrasing corpus.

Drives every case in evals/phrasings.toml through a real Call with the configured
interpreter and compares the model's reading of each caller turn with the expected
reading. Prints per-stage reading accuracy, yes/no answer accuracy on the
confirmation stages, median and p95 reading latency, and how often the call then
did the expected thing. Exits 1 if any ``must_pass`` case
is misread. Skips (exit 0) with a message when no model is reachable.

    uv run python -m evals.run_phrasings                 # Ollama, SWITCHBOARD_OLLAMA_MODEL
    uv run python -m evals.run_phrasings --interpreter claude
    uv run python -m evals.run_phrasings --stage proxy_permission --case permission-yes-i-do

Behavior is reported but not gated: a correct reading can still be mishandled by the
code, which tests/test_phrasings.py tracks with xfail marks.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

import httpx

from evals.phrasings import (
    Case,
    TurnRecord,
    answer_ok,
    behavior_failures,
    load,
    play,
    reading_failures,
)
from switchboard.intents import ClaudeRouter, OllamaInterpreter
from tests.test_gaps import Clinic


@dataclass
class Result:
    case: Case
    records: list[TurnRecord]
    misread: list[tuple[TurnRecord, list[str]]]
    behavior: list[str]


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


async def interpreter(kind: str, client: httpx.AsyncClient):
    """The configured interpreter and its description, or None and why it is unavailable."""
    if kind == "claude":
        key, model = os.getenv("ANTHROPIC_API_KEY"), os.getenv("SWITCHBOARD_CLAUDE_MODEL")
        if not key or not model:
            return None, "set ANTHROPIC_API_KEY and SWITCHBOARD_CLAUDE_MODEL to use Claude"
        return ClaudeRouter(client, key, model), f"Claude {model}"
    model = os.getenv("SWITCHBOARD_OLLAMA_MODEL", "qwen2.5:3b")
    port = int(os.getenv("SWITCHBOARD_OLLAMA_PORT", "11434"))
    try:
        response = await client.get(f"http://127.0.0.1:{port}/api/tags", timeout=3)
        response.raise_for_status()
        names = {item.get("name") for item in response.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return None, f"no Ollama server on port {port}"
    if model not in names and f"{model}:latest" not in names:
        return None, f"Ollama has no model {model} (have: {', '.join(sorted(names)) or 'none'})"
    return OllamaInterpreter(client, model, port), f"Ollama {model}"


async def evaluate(cases: list[Case], setups, model, root: Path) -> list[Result]:
    results = []
    for index, case in enumerate(cases, 1):
        directory = root / case.id
        directory.mkdir()
        session = Clinic(directory, existing=setups[case.setup].existing).session()
        records = await play(case, setups, session, model)
        misread = [(record, reading_failures(record)) for record in records]
        results.append(
            Result(
                case,
                records,
                [(record, problems) for record, problems in misread if problems],
                behavior_failures(case, session, records),
            )
        )
        print(f"\r{index}/{len(cases)} cases", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return results


def report(results: list[Result], label: str) -> int:
    turns: dict[str, list[tuple[TurnRecord, bool]]] = defaultdict(list)
    cases: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        cases[result.case.stage].append(result)
        failed = {id(record) for record, _ in result.misread}
        for record in result.records:
            turns[record.stage].append((record, id(record) not in failed))
    print(f"\nPhrasing corpus against {label}\n")
    header = (
        f"{'stage':<18} {'turns':>5} {'read ok':>8} {'acc':>6} {'yes/no':>7} "
        f"{'median':>8} {'p95':>8} {'behaves':>8}"
    )
    print(header)
    print("-" * len(header))
    all_seconds = []
    for stage in sorted(turns, key=lambda name: list(turns).index(name)):
        rows = turns[stage]
        ok = sum(passed for _, passed in rows)
        seconds = [record.seconds for record, _ in rows if record.seconds is not None]
        all_seconds += seconds
        stage_cases = cases.get(stage, [])
        behaves = (
            f"{sum(not r.behavior for r in stage_cases)}/{len(stage_cases)}" if stage_cases else "-"
        )
        scored = [answer_ok(record) for record, _ in rows]
        scored = [ok for ok in scored if ok is not None]
        yes_no = f"{sum(scored)}/{len(scored)}" if scored else "-"
        print(
            f"{stage:<18} {len(rows):>5} {ok:>8} {ok / len(rows):>6.0%} {yes_no:>7} "
            f"{median(seconds) * 1000 if seconds else 0:>6.0f}ms {p95(seconds) * 1000 if seconds else 0:>6.0f}ms "
            f"{behaves:>8}"
        )
    total = sum(len(rows) for rows in turns.values())
    ok = sum(passed for rows in turns.values() for _, passed in rows)
    print("-" * len(header))
    scored = [answer_ok(record) for rows in turns.values() for record, _ in rows]
    scored = [ok for ok in scored if ok is not None]
    print(
        f"{'all':<18} {total:>5} {ok:>8} {ok / total:>6.0%} {f'{sum(scored)}/{len(scored)}':>7} "
        f"{median(all_seconds) * 1000 if all_seconds else 0:>6.0f}ms "
        f"{p95(all_seconds) * 1000 if all_seconds else 0:>6.0f}ms "
        f"{sum(not r.behavior for r in results)}/{len(results)}"
    )
    print("\n'read ok' counts caller turns, by the stage they answered. 'behaves' counts cases")
    print("whose call did the expected thing end to end with this model (not gated).")
    print("'yes/no' counts turns on confirmation stages whose answer to the question was")
    print("read correctly (yes / no / unclear).")
    if scored:
        print(
            f"\nYes/no accuracy on confirmation stages: {sum(scored)}/{len(scored)} "
            f"({sum(scored) / len(scored):.1%})"
        )

    misread = [result for result in results if result.misread]
    if misread:
        print("\nMisread cases:")
        for result in misread:
            gate = "MUST PASS" if result.case.must_pass else "ambiguous"
            for record, problems in result.misread:
                got = record.meaning.model_dump() if record.meaning else None
                print(f"  [{gate}] {result.case.id} ({record.stage}) {record.turn.say!r}")
                print(f"      {'; '.join(problems)}; got {got}")
    gated = [result for result in misread if result.case.must_pass]
    yes_no_misread = [
        result
        for result in gated
        if any(answer_ok(record) is False for record, _ in result.misread)
    ]
    print(f"\n{len(gated)} must_pass case(s) misread out of {len(results)} cases.")
    print(f"{len(yes_no_misread)} must_pass case(s) with a misread yes/no answer.")
    return 1 if gated else 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--interpreter", choices=("ollama", "claude"), default="ollama")
    parser.add_argument("--stage", action="append", help="only cases at this stage")
    parser.add_argument("--case", action="append", help="only this case id")
    args = parser.parse_args()
    corpus = load()
    cases = [
        case
        for case in corpus.cases
        if (not args.stage or case.stage in args.stage) and (not args.case or case.id in args.case)
    ]
    if not cases:
        print("No cases match.")
        return 1
    async with httpx.AsyncClient() as client:
        model, label = await interpreter(args.interpreter, client)
        if model is None:
            print(f"Skipping phrasing eval: {label}.")
            return 0
        with TemporaryDirectory() as directory:
            results = await evaluate(cases, corpus.setups, model, Path(directory))
    return report(results, label)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
