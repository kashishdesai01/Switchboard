"""Live evaluation of the Claude phone agent against a fresh demo store.

Plays scripted callers (including transcripts reported from the live line) and
simulated callers (a second Claude call with a persona and a goal) against
``AgentCall`` with the real model, then checks the store: which patient, which
slot, what was written. Prints per-case pass/fail, turn latency and token use.

Run with the key in the environment:
    uv run python -m evals.run_agent [--only ID ...] [--no-sim] [--no-scripts]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from switchboard.agent import AgentCall
from switchboard.agent.engine import agent_model
from switchboard.config import BAYVIEW
from switchboard.fixtures import synthetic_resources
from switchboard.frontdesk import FrontDesk, MailSettings
from switchboard.operations import Operations
from switchboard.scheduling import Scheduling, instant
from switchboard.store import DemoStore, Resource

# 08:00 on Tuesday, November 10 at the clinic, as in the tests.
CLOCK = datetime(2026, 11, 10, 16, tzinfo=UTC)
TODAY = CLOCK.astimezone(BAYVIEW.timezone).date()
# Haiku 5.5 list price per million tokens (prompts under 100K tokens).
PRICE_IN, PRICE_OUT = 0.10, 0.50
MAX_SIM_TURNS = 16

EXTRA_PATIENTS = {
    "robert-smith": ("Robert", "Smith", "1950-03-03"),
    "dana-whitfield": ("Dana", "Whitfield", "1985-08-18"),
}
# Words that must never be spoken.
NEVER_SAY = re.compile(r"\*\*|^\s*[-#]|couldn't follow|\btools?\b|json|option \d", re.I | re.M)


def local(item: Resource) -> datetime:
    return instant(item["start"]).astimezone(BAYVIEW.timezone)


def weekday(name: str, weeks: int = 0) -> date:
    """The next given weekday after today."""
    target = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(name[:3].lower())
    ahead = (target - TODAY.weekday()) % 7 or 7
    return TODAY + timedelta(days=ahead + 7 * weeks)


# Recording client


class Recorder:
    """Wraps AsyncAnthropic: counts calls and tokens, keeps every tool use."""

    def __init__(self, client: Any):
        self.client = client
        self.messages = self
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.tool_uses: list[tuple[str, dict]] = []

    async def create(self, **params: Any) -> Any:
        self.calls += 1
        response = await self.client.messages.create(**params)
        self.count(response)
        for block in response.content:
            if block.type == "tool_use":
                self.tool_uses.append((block.name, dict(block.input)))
        return response

    def count(self, response: Any) -> None:
        usage = response.usage
        self.tokens_in += (
            usage.input_tokens
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
        self.tokens_out += usage.output_tokens


# A fresh clinic per case


class World:
    def __init__(self, root: Path, client: Any, existing: dict[str, int]):
        resources = synthetic_resources(BAYVIEW, CLOCK)
        for pid, (given, family, born) in EXTRA_PATIENTS.items():
            resources.append(
                {
                    "resourceType": "Patient",
                    "id": pid,
                    "active": True,
                    "name": [{"given": [given], "family": family}],
                    "birthDate": born,
                }
            )
        slots = [item for item in resources if item["resourceType"] == "Slot"]
        # Existing follow-ups, three days out and later, well past the notice window.
        for index, (pid, days) in enumerate(existing.items()):
            day = TODAY + timedelta(days=days)
            slot = min(
                (
                    s
                    for s in slots
                    if local(s).date() == day
                    and s["status"] == "free"
                    and instant(s["end"]) - instant(s["start"]) == timedelta(minutes=20)
                ),
                key=local,
            )
            slot["status"] = "busy"
            provider = slot["schedule"]["reference"].removeprefix("Schedule/schedule-")
            resources.append(
                {
                    "resourceType": "Appointment",
                    "id": f"existing-{index}",
                    "status": "booked",
                    "start": slot["start"],
                    "end": slot["end"],
                    "slot": [{"reference": f"Slot/{slot['id']}"}],
                    "participant": [
                        {"actor": {"reference": f"Patient/{pid}"}, "status": "accepted"},
                        {"actor": {"reference": f"Practitioner/{provider}"}, "status": "accepted"},
                    ],
                }
            )
        self.store = DemoStore(root / "s.sqlite")
        self.store.seed(resources)
        self.scheduling = Scheduling(self.store, BAYVIEW, now=lambda: CLOCK)
        self.ops = Operations(root / "ops.sqlite", "eval")
        self.frontdesk = FrontDesk(self.ops, MailSettings())
        self.ops.frontdesk = self.frontdesk
        self.recorder = Recorder(client)
        self.call = AgentCall(
            self.scheduling,
            self.recorder,
            self.ops.start("eval"),
            voice=True,
            allow_registration=True,
            frontdesk=self.frontdesk,
            model=agent_model(),
            timeout=15.0,
        )
        self.latencies: list[float] = []
        self.transcript: list[str] = []

    async def booked(self) -> dict[str, dict]:
        """Booked appointments by (patient, start), with patient name and provider."""
        result = {}
        for item in await self.store.search("Appointment"):
            if item["status"] != "booked":
                continue
            refs = [p["actor"]["reference"] for p in item["participant"]]
            pid = next(r for r in refs if r.startswith("Patient/")).removeprefix("Patient/")
            provider = next(
                (r for r in refs if r.startswith("Practitioner/")), "Practitioner/?"
            ).removeprefix("Practitioner/")
            patient = await self.store.read("Patient", pid)
            name = patient["name"][0]
            spoken = name.get("text") or " ".join([*name.get("given", []), name.get("family", "")])
            result[f"{pid}@{item['start']}"] = {
                "patient": pid,
                "name": spoken.strip(),
                "start": local(item),
                "provider": provider,
            }
        return result

    async def held(self) -> int:
        return sum(1 for s in await self.store.search("Slot") if s["status"] == "busy-tentative")

    async def say(self, words: str):
        self.call.playback_finished(self.call.last_reply.id if self.call.last_reply else None)
        self.transcript.append(f"  caller: {words}")
        started = time.monotonic()
        reply = await self.call.handle(words)
        self.latencies.append(time.monotonic() - started)
        self.transcript.append(f"  agent:  {reply.text}")
        return reply


# Expectations


@dataclass
class Expect:
    """The end state: which appointments were added/removed, and the outcome."""

    outcome: str | None = None  # must appear in call.outcomes
    added: int = 0
    removed: int = 0
    patient: str | None = None  # patient id, or "name:Full Name" for a new patient
    slot: Callable[[dict], bool] | None = None  # on the added appointment
    slot_text: str = ""
    no_outcomes: tuple[str, ...] = ()


async def check(world: World, before: dict, expect: Expect) -> list[str]:
    problems = []
    after = await world.booked()
    added = [after[k] for k in after.keys() - before.keys()]
    removed = [before[k] for k in before.keys() - after.keys()]
    if len(added) != expect.added or len(removed) != expect.removed:
        problems.append(
            f"expected +{expect.added}/-{expect.removed} appointments, got "
            f"+{len(added)}/-{len(removed)}"
        )
    for item in added + removed:
        if expect.patient and expect.patient.startswith("name:"):
            ok = item["name"].lower() == expect.patient[5:].lower()
        else:
            ok = expect.patient is None or item["patient"] == expect.patient
        if not ok:
            problems.append(f"wrong patient {item['patient']} ({item['name']})")
    if expect.slot and added and not expect.slot(added[0]):
        problems.append(
            f"slot {added[0]['start']:%a %b %d %H:%M} with {added[0]['provider']} is not "
            f"{expect.slot_text}"
        )
    outcomes = world.call.outcomes
    if expect.outcome and expect.outcome not in outcomes:
        problems.append(f"outcome {expect.outcome} missing from {outcomes}")
    for bad in expect.no_outcomes:
        if bad in outcomes:
            problems.append(f"unexpected outcome {bad}")
    if await world.held():
        problems.append("a slot is still held")
    return problems


# Scripted callers


@dataclass
class Step:
    say: str
    # Wait for the agent to reach this point first; meanwhile answer with the script's filler.
    wait: str | None = None  # "options" | "confirm"
    # A check on the reply to this step: returns a problem or None.
    check: Callable[[Any, World], str | None] | None = None


@dataclass
class Script:
    id: str
    steps: list[str | Step]
    expect: Expect
    existing: dict[str, int] = field(default_factory=dict)
    filler: str = "Whatever is soonest is fine."
    forbid_tool: Callable[[str, dict], str | None] | None = None


def waiting(call: AgentCall, wait: str | None) -> bool:
    reply = call.last_reply
    if wait == "confirm":
        return not (reply and reply.confirmation)
    if wait == "options":
        return not call.options or bool(reply and reply.confirmation)
    return False


def asks_year(reply, world) -> str | None:
    text = reply.text.lower()
    # Replies are spoken, so years arrive as words ("twenty nineteen").
    invented = r"\b(2019|2008|1919|1908|twenty nineteen|two thousand eight|nineteen (?:nineteen|oh eight))\b"
    if re.search(invented, text):
        return f"invented a year: {reply.text!r}"
    return None if "year" in text or "birth" in text else f"no year question: {reply.text!r}"


def mentions_hours(reply, world) -> str | None:
    text = reply.text.lower()
    said = ("9" in text or "nine" in text) and ("5" in text or "five" in text)
    return None if said else f"no hours: {reply.text!r}"


def maria_options_booking(pick: str, answer: str) -> list[str | Step]:
    return [
        "Hi, this is Maria Lopez, born July 2nd 1984. I'd like to book a follow-up for myself.",
        Step(pick, wait="options"),
        Step(answer, wait="confirm"),
    ]


def only_dob(expected: str) -> Callable[[str, dict], str | None]:
    def forbid(name: str, args: dict) -> str | None:
        if name == "verify_patient" and args.get("birth_date") != expected:
            return f"verify_patient with birth_date {args.get('birth_date')}"
        return None

    return forbid


SCRIPTS = [
    Script(
        "live-proxy-uncle",
        [
            "Hi. I want to schedule a doctor's appointment for my uncle.",
            "Yes. I do. I'm his guardian.",
            "I am his guardian. I can make the book appointment.",
            "Robert Smith.",
            "March 3rd, 1950.",
            Step("The first one please.", wait="options"),
            Step("Yes.", wait="confirm"),
        ],
        Expect("booked", added=1, patient="robert-smith"),
        filler="He's fine with any time, the soonest is good.",
    ),
    Script(
        "live-its-me",
        [
            "Hi, I need to book an appointment.",
            "It's me.",
            "The name is Mia Smith.",
            "June 5th, 1992.",
            Step("The first one please.", wait="options"),
            Step("Yes.", wait="confirm"),
        ],
        Expect("booked", added=1, patient="name:Mia Smith"),
    ),
    Script(
        "live-name-corrected",
        [
            "I'd like to book an appointment for myself. I'm a new patient.",
            "Barb Smith.",
            "April 9th, 1979.",
            Step("The first one please.", wait="options"),
            Step("Carl Smith.", wait="confirm"),
            Step("That's correct.", wait="confirm"),
        ],
        Expect("booked", added=1, patient="name:Carl Smith"),
    ),
    Script(
        "live-split-year",
        [
            "Hi, I want to book a follow-up for myself.",
            "Dana Whitfield.",
            Step("18th August 19", check=asks_year),
            Step("8", check=asks_year),
            "1985",
            Step("The first one please.", wait="options"),
            Step("Yes.", wait="confirm"),
        ],
        Expect("booked", added=1, patient="dana-whitfield"),
        forbid_tool=only_dob("1985-08-18"),
    ),
    Script(
        "live-options-fragments",
        maria_options_booking("The first one please.", "Yes."),
        Expect("booked", added=1, patient="maria-demo"),
    ),
    Script(
        "self-booking-provider",
        [
            "Hello, I'd like to make a follow-up appointment with Doctor Chen.",
            "It's for me. Maria Lopez.",
            "July 2nd, 1984.",
            Step("The second one works.", wait="options"),
            Step("Yes please.", wait="confirm"),
        ],
        Expect(
            "booked",
            added=1,
            patient="maria-demo",
            slot=lambda a: a["provider"] == "chen",
            slot_text="with Dr. Chen",
        ),
        filler="Any time with Doctor Chen is fine.",
    ),
    Script(
        "reschedule",
        [
            "Hi, I need to move my appointment.",
            "Maria Lopez, born July 2nd 1984.",
            Step("Yes, that one. Could I come in the afternoon instead?"),
            Step("The first one.", wait="options"),
            Step("Yes.", wait="confirm"),
        ],
        Expect(
            "rescheduled",
            added=1,
            removed=1,
            patient="maria-demo",
            slot=lambda a: a["start"].hour >= 12,
            slot_text="in the afternoon",
        ),
        existing={"maria-demo": 3},
        filler="Any afternoon is fine.",
    ),
    Script(
        "cancel",
        [
            "I need to cancel my appointment.",
            "Maria Lopez.",
            "July 2nd 1984.",
            Step("Yes.", wait="confirm"),
        ],
        Expect("cancelled", removed=1, patient="maria-demo"),
        existing={"maria-demo": 3},
        filler="Yes, that one.",
    ),
    Script(
        "change-mind-day",
        [
            "Hi, Maria Lopez, July 2nd 1984. I'd like a follow-up on Thursday.",
            Step("Actually, Friday would be better.", wait="options"),
            Step("The first one.", wait="options"),
            Step("Yes.", wait="confirm"),
        ],
        Expect(
            "booked",
            added=1,
            patient="maria-demo",
            slot=lambda a: a["start"].date() == weekday("fri"),
            slot_text=f"on Friday {weekday('fri')}",
        ),
        filler="Friday, any time.",
    ),
    Script(
        "no-at-confirmation",
        maria_options_booking("The first one.", "No."),
        Expect("declined", no_outcomes=("booked",)),
    ),
    Script(
        "hedged-yes",
        [
            *maria_options_booking("The first one.", "Um, I think so, maybe?"),
            "Let me check with my wife first.",
        ],
        Expect(no_outcomes=("booked",)),
    ),
    Script(
        "hours-mid-flow",
        [
            "Hi, I want to book a follow-up.",
            "Maria Lopez, July 2nd 1984.",
            Step("Wait, what are your hours, are you open on weekends?", check=mentions_hours),
            "Okay, thanks. Any afternoon is fine.",
            Step("The first one.", wait="options"),
            Step("Yes.", wait="confirm"),
        ],
        Expect("booked", added=1, patient="maria-demo"),
        filler="Any afternoon is fine.",
    ),
    Script(
        "emergency",
        [
            Step(
                "I'm having crushing chest pain and I can't breathe.",
                check=lambda r, w: None if w.recorder.calls == 0 else "the model was consulted",
            ),
        ],
        Expect("emergency"),
    ),
]


@dataclass
class Result:
    id: str
    kind: str
    problems: list[str]
    world: World
    fillers: int = 0

    @property
    def passed(self) -> bool:
        return not self.problems


async def run_script(script: Script, client: Any, root: Path) -> Result:
    world = World(root, client, script.existing)
    before = await world.booked()
    problems: list[str] = []
    fillers = 0
    world.transcript.append(f"  agent:  {world.call.greeting().text}")
    try:
        for step in script.steps:
            step = Step(step) if isinstance(step, str) else step
            if world.call.stage == "closed":
                break
            for _ in range(2):
                if not waiting(world.call, step.wait) or world.call.stage == "closed":
                    break
                fillers += 1
                await world.say(script.filler)
            if waiting(world.call, step.wait):
                problems.append(f"never reached {step.wait} before {step.say!r}")
                break
            reply = await world.say(step.say)
            if step.check and (problem := step.check(reply, world)):
                problems.append(problem)
        # The caller hangs up first: any slot still held must be released by then.
        await world.call.close()
        problems += await check(world, before, script.expect)
    except Exception as exc:  # noqa: BLE001 - report and move on
        problems.append(f"harness error {type(exc).__name__}: {exc}")
    problems += spoken_problems(world)
    if script.forbid_tool:
        problems += [p for n, a in world.recorder.tool_uses if (p := script.forbid_tool(n, a))]
    await world.call.close()
    return Result(script.id, "script", problems, world, fillers)


def spoken_problems(world: World) -> list[str]:
    return [
        f"bad spoken text: {line.strip()!r}"
        for line in world.transcript
        if line.startswith("  agent:") and NEVER_SAY.search(line.removeprefix("  agent:"))
    ]


# Simulated callers


@dataclass
class Persona:
    id: str
    persona: str
    expect: Expect
    existing: dict[str, int] = field(default_factory=dict)


SIMULATED = [
    Persona(
        "sim-rambling",
        "You are Maria Lopez, born July 2, 1984. You ramble: you mention your knee, your "
        "dog and your busy week before getting to the point. You want a follow-up with "
        "Doctor Brooks, in the morning, any day next week or this week. Accept the first "
        "morning time offered with Doctor Brooks.",
        Expect(
            "booked",
            added=1,
            patient="maria-demo",
            slot=lambda a: a["provider"] == "brooks" and a["start"].hour < 12,
            slot_text="a morning with Dr. Brooks",
        ),
    ),
    Persona(
        "sim-terse",
        "You are Maria Lopez, born July 2, 1984. You are terse: two to four words per "
        "reply. You want to cancel your upcoming appointment.",
        Expect("cancelled", removed=1, patient="maria-demo"),
        existing={"maria-demo": 4},
    ),
    Persona(
        "sim-corrects-self",
        "You are Dana Whitfield. When asked your birth date, first say August 18th, 1958, "
        "then immediately in your next reply say 'sorry, I mean 1985'. Your real birth date "
        "is August 18, 1985. You want to move your upcoming appointment to an afternoon. "
        "Accept the first afternoon time offered.",
        Expect(
            "rescheduled",
            added=1,
            removed=1,
            patient="dana-whitfield",
            slot=lambda a: a["start"].hour >= 12,
            slot_text="in the afternoon",
        ),
        existing={"dana-whitfield": 5},
    ),
    Persona(
        "sim-changes-mind",
        "You are Tom Becker, born February 20, 1990, a new patient who has never been to the "
        f"clinic. You want a first visit. First ask for Monday {weekday('mon'):%B} "
        f"{weekday('mon').day}. After the assistant offers Monday times, change your mind: "
        f"say Wednesday {weekday('wed'):%B} {weekday('wed').day} works better. Accept the first "
        "Wednesday time.",
        Expect(
            "booked",
            added=1,
            patient="name:Tom Becker",
            slot=lambda a: a["start"].date() == weekday("wed"),
            slot_text=f"on Wednesday {weekday('wed')}",
        ),
    ),
    Persona(
        "sim-asks-hours",
        "You are calling to book an appointment for your uncle, Robert Smith, born March 3, "
        "1950. He asked you to book it for him, so you have his permission. Partway through, "
        "before choosing a time, ask what the clinic hours are and whether they take Medicare. "
        "Then go back to booking and accept the first time offered.",
        Expect("booked", added=1, patient="robert-smith"),
    ),
]

CALLER_RULES = """You are role-playing a person phoning a clinic's automated assistant.
{persona}
Rules: reply with only the words you say out loud, one or two short sentences, like a real \
phone caller. Do not narrate. Answer what you are asked. When the assistant confirms your \
request is done and asks if there is anything else, say no thanks. When the conversation is \
over (the assistant said goodbye, or you have said goodbye), reply with exactly [HANGUP]."""


async def run_simulated(persona: Persona, client: Any, root: Path) -> Result:
    world = World(root, client, persona.existing)
    caller = Recorder(client)
    before = await world.booked()
    problems: list[str] = []
    system = CALLER_RULES.format(persona=persona.persona)
    reply = world.call.greeting()
    world.transcript.append(f"  agent:  {reply.text}")
    history: list[dict] = []
    try:
        for _ in range(MAX_SIM_TURNS):
            history.append({"role": "user", "content": reply.text})
            response = await caller.create(
                model=agent_model(), max_tokens=150, system=system, messages=history
            )
            words = " ".join(b.text for b in response.content if b.type == "text").strip()
            history.append({"role": "assistant", "content": words or "..."})
            if not words or "[HANGUP]" in words or world.call.stage == "closed":
                break
            reply = await world.say(words)
        await world.call.close()
        problems += await check(world, before, persona.expect)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"harness error {type(exc).__name__}: {exc}")
    problems += spoken_problems(world)
    await world.call.close()
    world.recorder.tokens_in += caller.tokens_in
    world.recorder.tokens_out += caller.tokens_out
    return Result(persona.id, "simulated", problems, world)


# Report


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def report(results: list[Result], verbose: bool) -> bool:
    print(f"Agent eval: model {agent_model()}\n")
    print(f"{'case':28} {'kind':10} {'result':6} {'turns':>5} {'median s':>8} {'calls':>5}")
    for item in results:
        lat = item.world.latencies
        med = f"{statistics.median(lat):.2f}" if lat else "-"
        status = "PASS" if item.passed else "FAIL"
        print(
            f"{item.id:28} {item.kind:10} {status:6} {len(lat):5} {med:>8} "
            f"{item.world.recorder.calls:5}" + (f"  fillers={item.fillers}" if item.fillers else "")
        )
        for problem in item.problems:
            print(f"    - {problem}")
    for item in results:
        if verbose or not item.passed:
            print(f"\nTranscript {item.id}:")
            print("\n".join(item.world.transcript))
    latencies = [x for item in results for x in item.world.latencies]
    scripts = [r for r in results if r.kind == "script"]
    sims = [r for r in results if r.kind == "simulated"]
    tokens_in = sum(r.world.recorder.tokens_in for r in results)
    tokens_out = sum(r.world.recorder.tokens_out for r in results)
    print()
    print(f"Scripts: {sum(r.passed for r in scripts)}/{len(scripts)} passed")
    print(f"Simulated callers: {sum(r.passed for r in sims)}/{len(sims)} reached the right outcome")
    if latencies:
        print(
            f"Turn latency: median {statistics.median(latencies):.2f} s, "
            f"p95 {percentile(latencies, 0.95):.2f} s over {len(latencies)} turns"
        )
    cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
    print(f"Tokens: {tokens_in} in, {tokens_out} out (about ${cost:.3f} at Haiku 5.5 list price)")
    return all(r.passed for r in scripts) and (
        not sims or sum(r.passed for r in sims) >= len(sims) - 1
    )


async def main(args: argparse.Namespace) -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; skipping the live agent eval.")
        return 0
    import anthropic

    client = anthropic.AsyncAnthropic(max_retries=2, timeout=15.0)
    limit = asyncio.Semaphore(args.parallel)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)

        async def bounded(index: int, job, case) -> Result:
            folder = root / f"{index}"
            folder.mkdir()
            async with limit:
                return await job(case, client, folder)

        jobs = []
        if not args.no_scripts:
            jobs += [(run_script, s) for s in SCRIPTS if not args.only or s.id in args.only]
        if not args.no_sim:
            jobs += [(run_simulated, p) for p in SIMULATED if not args.only or p.id in args.only]
        results = await asyncio.gather(
            *(bounded(i, job, case) for i, (job, case) in enumerate(jobs))
        )
        return 0 if report(list(results), args.verbose) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="case ids to run")
    parser.add_argument("--no-sim", action="store_true")
    parser.add_argument("--no-scripts", action="store_true")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("-v", "--verbose", action="store_true", help="print every transcript")
    sys.exit(asyncio.run(main(parser.parse_args())))
