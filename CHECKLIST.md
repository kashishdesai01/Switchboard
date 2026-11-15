# Switchboard gap fixes

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

- [ ] **Phase 7: Double booking and appointment paging.**
  Slot offers exclude times overlapping the patient's booked appointments (ignoring the one being moved), and the check is repeated at commit. Text mode can page through all appointments; with only one, the caller is asked to confirm it directly.
  Done: the double-booking and third-appointment gap tests pass.

- [ ] **Phase 8: Finish splitting `conversation.py`.**
  Move the stage handlers into modules, without changing behavior.
  Done: no file under `switchboard/` exceeds 700 lines; `tests/test_gaps.py` has no xfail marks left; baseline green.
