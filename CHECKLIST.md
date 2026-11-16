# Switchboard: Claude runs the phone conversation

Outcome: on the Vapi phone line, Claude holds the whole conversation and calls tools to look up patients, find times and make changes. Code no longer scripts the questions; it only guards the writes. A caller can book, reschedule or cancel in their own words, correct themselves at any point, answer in pieces or talk over the bot, and the call still completes.

Design rules (every phase):
- New code lives in `switchboard/agent/`. The existing scripted `Call` (`switchboard/call/`) stays untouched and remains the engine for the browser lab, Twilio and any transport not switched. Do not edit `switchboard/call/` except to extract a shared helper without behavior change.
- Use the official `anthropic` Python SDK (`AsyncAnthropic`, add `anthropic` to pyproject dependencies with `uv add anthropic`). SDK reference: the official Anthropic Python SDK docs (tool use). Do not guess SDK names. Model from `SWITCHBOARD_AGENT_MODEL`, falling back to `SWITCHBOARD_CLAUDE_MODEL` (currently `claude-haiku-5-5`). Key from `ANTHROPIC_API_KEY`. Never print or log the key.
- Guarantees enforced in tool code, not the prompt: (a) no appointment data is read or changed until the patient is verified by `Scheduling.identify` (or drafted as a new demo patient when `allow_registration` and booking); (b) booking for someone else requires the caller to have stated permission, and only where the scripted call allows proxies today (`frontdesk` set and the store is a `DemoStore`); (c) a write (`Scheduling.commit`) happens only after a proposal was read to the caller in an earlier assistant turn and the caller replied after it, and only for exactly the proposed slot/appointment; (d) the existing rules-based emergency check (`switchboard.safety`) runs on every caller turn before the model and, when it triggers, the call gives the same 911 guidance as today without consulting the model; (e) birth dates passed to tools must be full YYYY-MM-DD dates.
- The commit path must reuse what the scripted call does on commit (ops mutation tracking via `trace.begin_mutation`/`finish_mutation`, front desk notices/email, `needs_review` on uncertain writes, hold release on decline/hangup). Read `switchboard/call/confirmation.py` and `switchboard/call/offers.py` and reuse or extract; do not reimplement differently.
- Replies are spoken: short, one question at a time, no markdown, times said the way the scripted call says them (reuse its describe/spoken helpers).
- Baseline before phase 1: `uv run pytest -q` → 571 passed, 3 skipped; `uv run python -m evals.run_text` → no failures, 0 safety violations; `uv run ruff check .` and `uv run ruff format --check .` clean. Every phase keeps it green.

- [x] **Phase 1: Agent engine and tools, tested with a fake model.**
  Add `switchboard/agent/` with an `AgentCall` that exposes the same interface transports use on `Call`: `greeting()`, `async handle(text) -> Reply`, `async digit(d)`, `async close()`, `trace`, `stage`, `playback_finished(reply_id)`, `interrupt()`, `summary_outcome()`, returning the existing `Reply` type (outcome, ends_call, confirmation, calendar). It keeps the conversation history, calls Claude with a system prompt describing the clinic (from `Clinic` config: name, hours, locations, providers, accepted insurance) and the tools, runs tool calls in a loop (cap at 5 tool rounds per caller turn), and returns Claude's final spoken text. Tools at minimum: `verify_patient(full_name, birth_date, for_self, permission_stated)`, `find_times(visit_type, preferences...)` mapping onto `Scheduling.options`, `list_appointments()`, `propose_booking(option)`, `propose_reschedule(appointment, option)`, `propose_cancel(appointment)`, `confirm_change()`, `transfer_to_staff(reason)`, `end_call()`. Tool results are short JSON the model reads. Model or network failure: say a short apology and retry once on the next turn; after two failures in a row hand off to staff (outcome `handoff`).
  Add `tests/test_agent.py` with a scripted fake Anthropic client (returns predetermined tool_use / text blocks) proving each guarantee: confirm_change refused in the same turn as the proposal; confirm_change refused for an unverified patient; proxy refused without permission and outside the demo store; emergency text never reaches the model and returns the 911 reply; a successful book writes exactly the proposed slot, records the mutation in the trace, and ends the task with outcome `booked`; hangup (`close`) releases a held slot; model failure path hands off after two failures.
  Done: `uv run pytest -q tests/test_agent.py` passes with at least the cases above; baseline green.
  Result: `switchboard/agent/engine.py` (AgentCall) and `tools.py` (verify_patient, find_times, list_appointments, propose_booking/reschedule/cancel, confirm_change, decline_change, transfer_to_staff, report_emergency, end_call). Commit path extracted to `commit_change`/`confirmation_notice` in confirmation.py with no behavior change. New-patient read-back says the name and birth date. 15 agent tests; baseline 586 passed, 3 skipped; run_text clean; ruff clean.

- [x] **Phase 2: Wire the agent into the Vapi line.**
  `VapiCalls.open` creates an `AgentCall` when `SWITCHBOARD_CALL_ENGINE=agent` (and an Anthropic key is configured), otherwise the scripted `Call` as today. Keypad entries still work (pass digits through as text the agent can read, e.g. "User keyed: 19840702"). Read-back interruption handling in `vapi.py` stays (an interrupted proposal means `confirm_change` is refused until the proposal is heard again). Ops trace, transcripts and the Operations console show agent calls like scripted ones (outcomes, staff work items for handoff/emergency/needs_review). `docs/vapi.md` documents the switch.
  Add Vapi-level tests (like `tests/test_vapi.py`, fake Anthropic client) covering a full booking, two caller messages in one request, an interrupted proposal, an emergency, and hangup.
  Done: `uv run pytest -q tests/test_vapi.py tests/test_agent.py` pass with the new cases; with `SWITCHBOARD_CALL_ENGINE` unset all existing tests are unchanged and green; baseline green.
  Result: `SWITCHBOARD_CALL_ENGINE=agent` + `ANTHROPIC_API_KEY` builds one shared AsyncAnthropic client; `VapiCalls.open` makes an AgentCall; keypad digits reach the agent as "User keyed: N"; the agent is told the first message Vapi spoke. `tests/test_vapi_agent.py` 8 cases. Fixed a pre-existing race in test_frontdesk's operations test (waits for the saved call record). Baseline 594 passed, 3 skipped (3 runs); run_text clean; ruff clean.

- [x] **Phase 3: Live evaluation against real Claude.**
  Add `evals/run_agent.py`: plays caller scripts against `AgentCall` on a fresh demo store with the real model and checks the end state (which patient, which slot, booked/cancelled/rescheduled/no write, handoff). Scripts must include the five reported live calls (proxy for uncle with "Yes I do"; "It's me." then a name; name corrected at the read-back; birth year split "18th August 19" / "8" / "1985"; answering during the options), self booking, reschedule, cancel, a caller who changes their mind about the day, a caller who says no at the final confirmation (no write), a hedged yes at the confirmation (no write), an off-topic question mid-flow (clinic hours) then back to booking, and an emergency. Also add 5 simulated callers where a second Claude call plays a caller persona with a goal and the harness checks the outcome. Report per-script pass/fail and per-turn latency (median, p95). Tune the system prompt and tool descriptions (not code paths for specific phrases) until scripts pass.
  Done: `uv run python -m evals.run_agent` (with the key from `~/.config/switchboard/vapi.env`) exits 0 with all scripted cases passing and at least 4 of 5 simulated callers reaching the right outcome; median turn latency printed (target ≤ 2.5 s); baseline green.
  Result: `evals/run_agent.py`, 13 scripts + 5 simulated callers on claude-haiku-5-5: 13/13 and 5/5 pass; median turn 1.5-1.6 s, p95 ~2.8-3.0 s; ~$0.07 per full run. Prompt rules added for corrections, self-by-default, searching right after verification. New patients are told they'll be set up, not "verified". Harness hangs up before checking holds. Baseline 594 passed, 3 skipped; run_text clean; ruff clean.

- [ ] **Phase 4: Switch the live line and verify.** (manager)
  Restart the Vapi server with `SWITCHBOARD_CALL_ENGINE=agent`, replay the reported calls through `/vapi/chat/completions` with the scratchpad `vcall.py`, and hand to the user for a live call.
  Done: replays book the right patient and slot with no "couldn't follow" or code prompts; server log shows 200s only.

---

# (Done) Switchboard: understand natural phrasing

Outcome: callers can answer each question in their own words ("yes I do", "yeah he said it's fine", a long explanation of who the visit is for), and the call understands them from context. The model reads each reply against the question that was just asked. Code still owns identity checks, holds, read-backs and every write. The handwritten parsers stay only as a fallback when the model is unavailable or times out, and as a cross-check where a wrong "yes" would change a booking.

Baseline before phase 1: `uv run pytest -q` → 389 passed, 3 skipped; `uv run python -m evals.run_text` → no failures; `uv run ruff check .` clean.
Every phase must keep that baseline green. From phase 3 the target model is Claude Haiku 5.5 through `ClaudeRouter` (`SWITCHBOARD_CLAUDE_MODEL=claude-haiku-5-5`, key in `~/.config/switchboard/vapi.env`); the live eval is `uv run python -m evals.run_phrasings --interpreter claude`. Ollama stays a supported fallback. Do not touch the uncommitted change in `tests/test_frontdesk.py`. No phrase-specific patches: no new entries in affirmative/name regex lists to make a single test case pass.

Reported failures this must fix (from live calls on 2026-11-15):
1. Permission question ("Do you have permission to book for them?"): "Yes, I do", "Yes I do", "yeah he said it's fine for me to book" are answered with "Please say yes or no." `_stage_proxy_permission` uses only `answer_to_confirmation` and ignores the model.
2. A split answer ("Yes." then "I have the permission.") lands on the name question, which hands the call to staff instead of re-asking.
3. Long openers ("I want to schedule an appointment for my uncle, he had a stroke a few months ago and was seen by Doctor Chen, ...") must route the same as the short version, keeping the provider preference.
4. A bare "Hi." at the greeting jumps to "Is the appointment for you or for someone else?".

- [x] **Phase 1: Phrasing corpus and two harnesses.**
  Add `evals/phrasings.toml`: for every stage that asks a question (at least intent opener, booking_recipient, proxy_permission, name, check_name, dob, check_dob, other_provider, day_alternative, select_slot, confirm_change, email_readback, anything_else), 8+ differently worded caller replies, each with the expected stage reading (yes / no / unclear, self / other, the name, the option number, the route, etc.). Include long replies, replies with extra detail, hedged or conditional replies that must stay unclear ("I think so, maybe?", "yes if it's after 3"), and the four reported failures above.
  Add `tests/test_phrasings.py`: drives a real `Call` through each case with a fake interpreter that returns the corpus's expected reading, and asserts the call does the right thing (advances, re-asks, declines). It tests that the code obeys a correct model reading. Cases that fail today are marked `xfail(strict=True)`.
  Add `evals/run_phrasings.py`: runs the same corpus against the configured real interpreter (Ollama by default, `SWITCHBOARD_OLLAMA_MODEL`), reports per-stage accuracy and median/p95 latency, and exits non-zero if any case marked `must_pass` fails. It skips cleanly with a message when no model is reachable.
  Result: 150 cases over 13 stages; 16 xfailed; qwen2.5:3b reads 73/143 turns (51%), median 4.7 s, p95 6.6 s (scratchpad phrasings_phase1_baseline.txt).
  Done: `uv run pytest -q tests/test_phrasings.py` exits 0 with the current failures reported as xfailed; `uv run python -m evals.run_phrasings` prints a per-stage accuracy table against qwen2.5:3b; baseline green.

- [x] **Phase 2: A stage-aware model reading.**
  Replace the bare `action/value` reading with a structured reading the model returns every turn: `answer` (yes / no / unclear / not_applicable) to the exact question just asked, the stage-specific `action` and `value`, `extra` (any additional request or detail the caller added, such as a provider or day), and the existing `emergency` / `needs_staff` flags. The model input includes the exact question the assistant last asked. Rewrite the instructions so ordinary agreement in any wording ("yes I do", "yeah he's fine with it", "I do, yes") is yes, while conditional, hedged, or contradictory replies are unclear. Keep the separate safety check unchanged. Both `OllamaInterpreter` and `ClaudeRouter` return the new reading; keep latency within the existing 8 s turn timeout (measure it).
  Result: qwen2.5:3b peaked at 81% yes/no (4.2 s median); qwen2.5:7b timed out on this 16 GB Mac. The user chose Claude Haiku 5.5 (`claude-haiku-5-5`): 92% of turns read correctly, yes/no 71/74 (95.9%), median 1.0 s, p95 1.3 s (scratchpad haiku_full.txt). 4 of its misreads were the router's 1.5 s timeout.
  Done: unit tests for the schema and prompt input pass; `uv run python -m evals.run_phrasings` shows yes/no accuracy at or above 95% on the confirmation stages with qwen2.5:3b and no `must_pass` failures among yes/no cases; median turn latency reported and no worse than 1 s above phase 1's; baseline green.

- [x] **Phase 3: Stages decide from the reading.**
  Every stage handler takes its answer from the model reading first and falls back to the handwritten parsers only when there is no reading. `_stage_proxy_permission`, `_stage_booking_recipient`, `_stage_name`, and the other confirmation stages go through one shared path, not each with its own regex. In `core.py`, a reading that answers the current question counts as a grounded reply, so `needs_staff` or a regex staff topic doesn't hand off a caller who answered the question. A reply that doesn't answer the question re-asks it in context (for example "Thanks. What's the patient's full name?") and only hands off after repeated failures, never on the first off-topic reply. Commits stay guarded: `confirm_change` still needs an unqualified yes, and the regex cross-check can veto a model "yes" there when the words are clearly qualified.
  Result (Haiku): 134/143 read, yes/no 72/74, behaves 135/140 then 139/140 after the plain-yes fix for other_provider/day_alternative; one timeout left on the long uncle opener (phase 4); "She's fine with it" is read unclear and re-asked (safe).
  Also: raise the `ClaudeRouter` turn timeout so a normal Haiku reply (p95 ~1.5 s) isn't dropped, and when both models are configured the server uses Claude for turn reading.
  Done: `tests/test_phrasings.py` has no xfail marks left for reported failures 1 and 2 and the stage cases; `uv run pytest -q` and `uv run python -m evals.run_text` green; `uv run python -m evals.run_phrasings --interpreter claude` has no timeouts and no `must_pass` yes/no failures.

- [x] **Phase 4: Openers.**
  A greeting with no request ("Hi.", "Hello, how are you?") gets a short reply asking how to help, not the booking-recipient question. Long, detailed openers are routed from the model reading (route, recipient, provider, day preferences), keeping the details the caller gave so they aren't asked again. Medical history given as the reason for a visit ("he had a stroke a few months ago") does not raise an emergency on its own, while the existing safety set stays at 100%.
  Result: no xfails left; 557 passed; Haiku: 136/143 read, yes/no 73/74, no timeouts, every intent/anything_else case behaves. Left: "it's for my husband, but I'd also like one for myself" books the husband first; "She's fine with it" re-asks.
  Done: reported failures 3 and 4 pass in `tests/test_phrasings.py` without xfail; `uv run pytest -q tests/test_safety.py tests/test_emergency.py` green; full baseline green; `uv run python -m evals.run_phrasings --interpreter claude` has no `must_pass` failures.

- [x] **Phase 5: End-to-end verification (manager).**
  Restart the Vapi server on the new code and replay the reported calls through `/vapi/chat/completions` with the real model.
  Result: server restarted on Claude Haiku (no Ollama); reported failures 1-4 replayed through /vapi/chat/completions with correct replies in 1.0-1.9 s; tunnel still 401 without the secret. Added: an opener that already says "for myself" skips the recipient question (3 corpus cases). pytest 560 passed / 3 skipped; run_text no failures, 0 safety violations; ruff clean. Live Haiku: 139/146 read, yes/no 73/74, 141/143 behave, no timeouts. run_phrasings exits 1 on 6 strict reading labels: 4 behave correctly (yes + preference reading, "July 02"), 2 are the known safe re-ask/ambiguous cases. Left open for the user.
  Done: each reported failure gets the right reply in the replay; `uv run pytest -q`, `uv run python -m evals.run_text`, `uv run python -m evals.run_phrasings --interpreter claude`, `uv run ruff check .` all pass.

---

# Previous: Switchboard gap fixes (done)

Outcome: every gap found in the 2026-11-15 review is covered by a test and fixed, and every test and eval stays green throughout.

Baseline before phase 1: `uv run pytest -q` → 158 passed, 3 skipped; `uv run python -m evals.run_text` → 64/64; `uv run ruff check .` clean.
Every phase must keep that baseline green, and must remove the xfail marks for the gaps it closes.

- [x] **Phase 1: Lock in every gap as a failing test.**
  Add `tests/test_gaps.py`, with one `xfail(strict=True)` test per finding: proxy phrasings ("someone else", "it's for John Smith", "my father needs an appointment", "I'm calling for my father…", "cancel my mom's appointment" vs "…for my mom"), clarifying dead end ("no, it was in the past, I need to book a follow-up"), missed emergencies (suicidal ideation, face/throat swelling, can't swallow, open fracture), double booking, third appointment unreachable in text, name in opener ("hi this is Maria Lopez"), call ends after hours/insurance/no-appointments.
  Done: `uv run pytest -q tests/test_gaps.py` exits 0 with every test reported as xfailed.

- [x] **Phase 2: Shared reply parsers.**
  Move language parsing out of `conversation.py` into `switchboard/understanding.py`. It covers: yes/no with the rest of the reply kept, recipient (self / other plus an optional name), name in the opening sentence, and booking verbs including "needs". Every stage uses these parsers. The model, when configured, fills the same structure.
  Done: the gap tests for proxy phrasing, clarifying dead end, name in opener and "needs" pass without xfail; baseline green.

- [x] **Phase 3: Triage levels and safety phrase set.**
  Triage levels: emergency (911), crisis (988), poisoning, urgent same-day, routine. The symptom list moves into a data file that includes orthopedic warning signs. Add a labelled safety set and a CI gate that requires 100% of the must-catch phrases to be caught.
  Done: `uv run pytest -q tests/test_safety.py` passes, including the safety set; the missed-emergency gap tests pass.

- [x] **Phase 4: Emergency handling.**
  Add the emergency notice to the greeting and a script for each level. Unclear answers to the clarifying question escalate. A separate safety check by the model runs on every turn and can only raise the level. Emergency and urgent results create an urgent work item and transfer to staff when possible.
  Done: new tests for each behavior pass; baseline green.

- [x] **Phase 5: Separate caller and patient.**
  Session state records caller and patient separately. Book, reschedule and cancel all ask who the patient is and get permission when the caller isn't the patient. Proxy policy is set per backend in the clinic config. Prompts say "the patient", and staff work items record the real request type.
  Done: the proxy gap tests pass, including the possessive and "for my mom" phrasings behaving the same way.

- [x] **Phase 6: More than one task per call.**
  Split outcomes into ones that end the call (emergency, handoff, goodbye) and ones that finish a task. After a finished task, ask "Anything else?" and keep the verified identity. Browser, Vapi and Twilio close only when the call ends. A call trace records several outcomes.
  Done: the call-continuation gap test passes; `tests/test_web.py` and `tests/test_vapi.py` are green.

- [x] **Phase 7: Double booking and appointment paging.**
  Slot offers exclude times overlapping the patient's booked appointments (ignoring the one being moved), and the check is repeated at commit. Text mode can page through all appointments; with only one, the caller is asked to confirm it directly.
  Done: the double-booking and third-appointment gap tests pass.

- [x] **Phase 8: Finish splitting `conversation.py`.**
  Move the stage handlers into modules, without changing behavior.
  Done: no file under `switchboard/` exceeds 700 lines; `tests/test_gaps.py` has no xfail marks left; baseline green.
