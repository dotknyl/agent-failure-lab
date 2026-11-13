# Pilot summary: context retention under automatic compaction

Pilot run on 2026-11-13. Raw recordings are in `runs/`, the table in `REPORT.md`, and the reviews with quoted
evidence in each run's `review.json`. This is a small pilot, not a benchmark.

## Setup

| | |
|---|---|
| Task | Read-only API incident investigation (`fixtures/checkout-latency-v1`, synthetic, ~567K chars) |
| Model / API | `gpt-6-luna`, OpenAI Responses API, `store=false`, `reasoning_effort=medium`; encrypted reasoning carried within a run, never between runs |
| Compaction | Application-managed model summary (frozen prompt v1); fires when estimated input reaches 25,000 tokens (`compaction_threshold_tokens` 37,000 including a 12,000 output reserve); keeps the latest complete tool group verbatim. A **stress setting**: the model's documented max input is 922,000 |
| Runs | 1 focused sanity run (no tools, curated excerpts) + 3 natural investigations, identical configuration |
| Cost | $0.058 total upper bound, including 3 endpoint probes and all summary calls (budget $3) |

## Results

| Run | Compactions | Re-reads after compaction | Timeline | Diagnosis | Competing | Mitigation | References | Cost |
|---|---|---|---|---|---|---|---|---|
| focused | n/a | n/a | pass | pass | pass | pass | pass | $0.0055 |
| natural 1 | 2 | 9 | pass | pass | pass | partial | pass | $0.0240 |
| natural 2 | 1 | 0 | pass | pass | pass | partial | pass | $0.0099 |
| natural 3 | 2 | 9 | pass | pass | pass | partial | pass | $0.0164 |

Mitigation is **partial** in all three natural runs by owner decision. Each identifies the correct option (a
reversible, approved rollback of only the payment-client profile v18 -> v17, with fraud screening and idempotency
preserved), but words it as conditional or non-committal ("make no change now", "evaluate", "if the problem
recurs"), while the ticket asks for a mitigation before recurrence. The focused run recommended it outright. That
difference is **not** attributed to context: the focused condition differs in tools, evidence selection and length.

## Observations (n = 3, one model, one incident)

1. **Compaction was exercised in every natural run** (5 compactions). No run stopped on a limit or an error, and
   all three reach the intended diagnosis: gateway degradation amplified by the v18 timeout/retry change, with
   the database reindex ruled out by span or metric evidence rather than timing alone.
2. **No evidence was observed to be lost at a decision point.** The audit flagged 10 critical facts as
   "literally lost" across the 5 compactions. Manual reading of every summary found all 10 present in paraphrase,
   with references. The probe misses came from rewording and, in one case, an en dash in a time range.
3. **Re-reading after compaction varied (9, 0, 9)** even though the summaries already carried the facts. Run 2
   answered immediately after compacting, citing lines carried in its summary. Runs 1 and 3 re-read artifacts,
   plausibly to obtain exact lines for citation. Whether the prompt's re-read sentence drives this was **not
   tested**.
4. **One minor present-but-unused case:** in run 1 the 2026-10-04 comparison under the old profile was in the
   final request (summary 2), but the answer did not use it and called the v18 contribution unquantified. Run 3
   used the same comparison.
5. **No context rot was demonstrated.** Nothing here shows degraded use of present context attributable to
   length or position, and the pilot was not designed to show that with three runs.

## Limitations

- **Sample size and scope:** 3 natural runs, one model at one reasoning effort, one synthetic incident, one
  fixture version, one summary prompt, one threshold. Nothing here estimates a failure rate or generalizes to
  other models, tasks or settings.
- **Token-estimate calibration:** the character heuristic was calibrated on the focused run (estimate/actual
  0.708 -> 2.12 chars/token), but in the natural runs it over-counted: ratio 1.15-1.41 on large requests, up to
  2.5 on the smallest. Compaction therefore fired at roughly 20-29K actual input tokens. The trigger and all
  per-request estimates are recorded next to provider usage.
- **Re-read prompting:** the frozen agent prompt says earlier context may be summarized and that artifacts can
  be read again. This likely encourages the re-reads counted above. It is disclosed and was not varied.
- **Encrypted reasoning:** each request carried 1-6 opaque encrypted reasoning items. They are recorded but not
  readable, so that part of every request is not auditable. They are dropped with any group compaction
  summarizes. All readable inputs (task, summaries, tool calls and results) are complete.
- **Evidence probes are literal:** the "facts lost" indicators produced only false alarms here (10 of 10).
  Conclusions rest on manual reading of the summaries and requests, recorded in `review.json`.
- **Review:** verdicts were drafted by the assistant from recorded evidence, with cited lines spot-checked, on
  the owner's instruction. The owner decided the mitigation verdict. This is not an independent human review.
- **Other:**
  - The threshold is far below the native window, by design.
  - Provider-side handling of requests is not observable.
  - Costs use the configured prices, so they are upper bounds: input is charged at the cache-write rate, and
    the probe and focused-run costs used an earlier, higher price setting.
  - Model outputs are stochastic; replay reproduces the harness, not the model.
