# Verification on November 13, 2026

The local turn evaluator ran against Ollama with Qwen2.5 3B on an Apple M3, using an in-process browser WebSocket and fresh SQLite calendars. It checked three synthetic calls.

| Scenario | Result | Turn response times |
| --- | --- | --- |
| Bayview booking with a spoken birth date and revised lunch preference | Offered two times in the requested week, selected the later slot by its actual start, required a fresh confirmation, and wrote the selected appointment | 2,716, 2,070, 2,018, 2,242, 2,696, 2,238, 1,919, 2,048 ms |
| Lakeside booking with remote stroke history followed by acute warning signs | Continued the booking after the historical mention, then routed the acute report to the emergency response without writing an appointment | 3,705, 2,602, 1 ms |
| Bayview caller running late | Routed to the front desk without writing an appointment | 4,147 ms |

Median across 12 turns was **2,240 ms**. Timing starts before sending caller text and ends when the reply arrives. It includes local model inference and calendar work. The 1 ms turn used the deterministic emergency check. This sample excludes transcription, synthesis, endpointing, audio playback and carrier transport.

Run from the project root with Ollama listening and `qwen2.5:3b` installed.

```bash
uv run python -m evals.verify_turn_model
```

The standard suite passed 126 tests with three HAPI checks run separately. All three passed against HAPI 8.8.0-1 and PostgreSQL 16. A separate loopback deployment test verified the browser WebSocket booking, its operation journal entry and the resulting FHIR Appointment. The text evaluator passed all 64 runs across both clinic configurations.

A later run with the same local model passed three new cases after changes to history and negation handling. Bayview booked the earlier of two lunch offers following a request for a specific doctor. Lakeside continued after hearing about a historical heart attack and routed a separate report of current chest pressure to the emergency response. Bayview handed an insurance update to staff. The median across these 11 local text turns was 4,251 ms.

The updated demo also passed a synthetic HTTP smoke test through its existing HTTPS tunnel. The test exercised Vapi authentication, a historical stroke mention followed by an acute report, and the end-of-call callback. The model timed out on the first attempt, and the deterministic fallback handled both turns. The trace confirmed that the configured model interpreted the scheduling turn on the warmed retry. The request used synthetic transcripts and a simulated callback. Handset speech and carrier latency remain unmeasured.

The signed Twilio replay uses a simulated carrier stream and simulated speech services. Live Twilio calls and voice latency require separate measurements.

A subsequent handset call reached the birthday stage. Its operation trace showed one retry, an accepted date, and an unconfirmed playback event at the identity read-back. Transcript capture was disabled. After the recipient and birthday fixes, three synthetic HTTPS cases passed for a booking on behalf of a relative, spoken date parsing with unconfirmed playback, and an acute report during recipient clarification. The suite passed 135 tests with three HAPI checks skipped in that run, and all 64 text evaluations passed.

Switchboard now requires the booking recipient to be established before collecting identity. A later run passed 136 tests, with three HAPI checks skipped, and all 64 text evaluations. Three new synthetic calls through the restarted Vapi HTTPS tunnel verified that a bare name did not bypass recipient clarification, a self-booking accepted a spoken date and recovered when playback went unconfirmed, and an emergency report took priority during recipient clarification.

A further synthetic Vapi HTTPS replay sent matching start and stop callbacks followed by an interruption. Switchboard rejected the first identity confirmation, accepted a fresh read-back after matching callbacks arrived in reverse order, then handled the caller's decline at the appointment proposal. This checks Switchboard's handling of callbacks sent through the tunnel. The updated flow still awaits a handset call to verify which events Vapi actually delivers during speech.

On November 14, two handset traces showed the deterministic detector closing the call on the opening turn before model interpretation. The call transcripts were unavailable. Switchboard now passes ambiguous condition mentions to the configured model, retains immediate routing for explicit acute reports, and clarifies when the model is unavailable. The configured Ollama service was offline. After restarting it, the 7B model timed out within the eight-second turn limit even after warm-up, so the demo was switched to Qwen2.5 3B. Three new synthetic Vapi HTTPS scenarios passed for a clinic name containing a condition, a follow-up concerning a relative's stroke, and an acute symptom during a follow-up request. The operation trace confirmed model interpretation. The local suite passed 147 tests with three HAPI checks skipped, and all 64 text evaluations passed. The revised flow still needs a handset retest.

The app now loads the configured Ollama model in the background on startup and refreshes its five-minute keep-alive every two minutes. When interpretation fails and the opening remains unclear, the app asks for a repeat once and then routes to staff. Tests cover unavailable, timed-out, and unusable interpretation results. A further tunnel run caught a model response that tried to hand off a recipient answer before asking permission. The recipient stage now owns that decision. After the fix, three different synthetic HTTPS calls passed with model use confirmed in Operations. The suite passed 151 tests with three HAPI checks skipped and all 64 text evaluations passed.

A further change restricted the emergency fast path for diagnosis labels. A diagnosis mention, a past-tense diagnosis with no recent timing, or a scheduling time elsewhere in a sentence goes to the configured model when context remains ambiguous. If the model cannot resolve it, Switchboard asks whether the condition is happening now. The bounded clarification preserves the previous prompt, treats acknowledgments as unresolved, and routes unresolved cases to the front desk with the emergency instruction. Three new synthetic Vapi HTTPS calls verified a clinic name with “today,” a historical seizure with a proxy request, and an explicit breathing emergency alongside a follow-up. Operations confirmed that the first two used the model and the final case took the immediate path. All 155 local tests and 64 text evaluations passed, with three HAPI tests skipped in that run.

The next replay found that the model could infer a booking from a diagnosis alone. Switchboard now asks about timing for that input and returns to the previous prompt after a negative answer. When the opening included an actual booking request, clarification preserves and reuses that request. Three fresh synthetic Vapi HTTPS cases passed for diagnosis-only clarification, proxy rehabilitation and decline, and an acute breathing report during scheduling. All 156 local tests and all 64 text evaluations passed, with three HAPI tests skipped.

The November 14 browser WebSocket rerun used the configured Qwen2.5 3B model. Exact-slot booking, historical context followed by an acute report, and late arrival routing passed. Median response time was 4,558.5 ms across 12 synthetic text turns. This excludes audio and carrier transport.

The full suite was then rerun against the isolated HAPI R4 and PostgreSQL stack on November 14. All 159 tests passed, including the three versioned FHIR integration cases, with no skips.

A November 14 handset trace confirmed three missing matching playback-start events after Switchboard captured a date of birth. Switchboard now adds a one-time spoken code to a repeated confirmation when playback evidence is unavailable. The code applies to that exact prompt and expires on interruption or replacement. The local simulator also supports booking for another fictional patient after the caller confirms permission. This path collects the patient's name and birth date and uses the same appointment confirmation and operation journal. It does not look up or update email, or permit access to the patient's existing appointments.

The local suite passed 158 tests with three HAPI checks skipped, and all 64 text evaluations passed. Adapter regression tests deliberately withheld every playback callback, confirmed same-prompt codes, rejected stale codes, and committed self and proxy bookings in isolated databases. Three synthetic calls through the running Vapi tunnel reached appointment offers for self and proxy bookings with missing playback callbacks and preserved emergency interruption during proxy authorization. The tunnel cases declined before writing appointments. Handset recognition of the spoken confirmation code remains unverified.
