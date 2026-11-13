# Pilot audit: from compaction boundary to decision

An offline audit of the recorded context-retention pilot, written on 2026-11-14. It reads the existing recordings
again with one question in mind: when the agent decided something after a compaction, what evidence did it
actually have, and does any defect in the decision trace back to context? No model was called, no run was
added, and no recorded verdict was changed. The pilot itself is described in [`PILOT_SUMMARY.md`](PILOT_SUMMARY.md);
the method in [`README.md`](README.md).

**Who did the analysis.** The manual readings, classifications and counts below are assistant-assisted analysis
(Claude Opus 5.5, on the owner's instruction), drawn from the recorded requests and summaries and spot-checked
against them. They are not independent human verification. The same applies to the pilot's `review.json` verdicts.

**Status.** The owner has decided to stop experimenting (2026-11-14). The one candidate experiment in section 6 is
documented and **deferred**: it is not implemented and no paid call has been made for it.

**Scope note.** The Revision 4 research brief and the previously proposed next phases are not stored in this
repository. This audit is assessed against the README's record of the brief (in particular "continuation variants
... deferred until a pilot finding justifies one") and the stated primary goal: diagnosing agent context
failures, not benchmarking compaction strategies or manufacturing failures.

Run names used below: **R1** = `20261113T163002Z-natural-8ea26b`, **R2** = `20261113T163542Z-natural-284440`,
**R3** = `20261113T163628Z-natural-a077c6`, **F** = the focused run. `seq` numbers refer to `events.jsonl` in that
run's directory.

## 1. Reassessment of next steps

| Candidate next step | Verdict | Reason |
|---|---|---|
| General matched-checkpoint A/B (summary vs. uncompacted history at the same point) | Not justified | No decision defect in the pilot is shown to depend on evidence lost in compaction (section 3), so there is no specific finding for it to explain. Run in general, it measures whether compaction changes answers, which is benchmarking. |
| Comparing compaction strategies (thresholds, retained groups, summary prompts) | Out of scope | Benchmarking. |
| Lower thresholds, harder or new fixtures, other models, more runs | Out of scope | These manufacture failures or estimate rates. Neither answers an open diagnostic question from the pilot. |
| One narrow check of whether summary *stance* carries into a decision | The only candidate; deferred | Section 6. Not implemented. |

## 2. Evidence-to-decision walkthrough

### Two kinds of retention

What a summary must carry falls into two kinds, and the pilot treats them differently:

- **Diagnostic evidence:** the facts that support or rule out a cause (configuration change, timings, spans,
  metrics). This is what `facts.json` covers. All five summaries kept the critical cause evidence the agent had
  gathered, reworded and with references.
- **Task and requester requirements:** what the requester asks for and under which constraints. These come from
  two places:
  - The **pinned task** is never compacted. It was in every request, including every final one, and it asks for
    "the smallest reversible mitigation" and forbids disabling payment safety checks. The requirement to
    recommend a reversible mitigation was therefore never lost.
  - The **ticket** adds a requester-specific qualifier on line 20: "We still need the cause and a mitigation
    before this happens again." All five summaries omitted it. What was lost is this timing qualifier, not the
    mitigation requirement.

The omission is observed. Its effect on any final decision is **not** established (sections 3 and 5).

### The five compaction boundaries

The summarizer sees only the groups it is removing (`SUMMARY_USER` in [`src/ctxlab/prompts.py`](src/ctxlab/prompts.py)).
The most recent group stays verbatim beside the summary.

| Boundary | Summarized → retained | What survived | What did not | What the agent did next |
|---|---|---|---|---|
| **R1 C1** (seq 48 → summary 51) | Requests 1-3 (7 small artifacts, broad searches) → request 4 group (checkout/db metrics L116-180, payment-log searches) | All gathered critical facts, in paraphrase with citations (10/10 audit flags across the pilot were probe misses, section 4) | The ticket's "We still need ... a mitigation before this happens again". The summary also says "`payment-client-log`, `db-metrics` ... remain unread", which the retained group contradicts | Narrow per-minute searches of the payment log and traces. That is new evidence, not repetition |
| **R1 C2** (seq 78 → 81) | Requests 4-5 → request 6 group (trace searches, runbook and provider re-reads) | Config, timeline, 8 s timeouts, attempt 4/4 exhaustions, Oct 4 comparison ("about 4.2 s"), DB values | The same ticket clause. The DB metric *column meanings* were never read, and the summary says so: "metric headers were not included in the excerpt, so interpret cautiously" | Read the db-metrics header (new), re-read DB rows L132-163 to interpret them and checkout rows L126-182, re-read 5 small artifacts, then answered |
| **R2 C1** (seq 50 → 53) | Requests 1-3 → request 4 group (payment searches in both trace files, payment-log searches) | Everything later used in the answer, including mitigation and safety requirements in paraphrase | Both halves of ticket line 20. The summary says "No content from traces-2026-10-04 was read", which the retained `payment in:traces-2026-10-04` search contradicts | Answered immediately, with no re-reads |
| **R3 C1** (seq 34 → 37) | Requests 1-2 (small artifacts only) → request 3 group (checkout/db metrics L1-120, searches) | Everything from the small artifacts | The same ticket clause. The summary says "Metrics, traces, payment-client log ... were listed but not inspected", which the retained group contradicts. This compaction only freed ~3.8K estimated tokens because the large group was the retained one, so C2 followed one request later | Continued the metrics from line 120, using the retained reads' `next_offset`. It did not re-read them |
| **R3 C2** (seq 52 → 55) | Request 3 group → request 4 group (metrics L120-199, slow traces L331-360, Oct 4 traces) | Oct 4 comparison ("4197 ms ... around 4.2 s"), DB values with their meaning, slow-trace span split | The same ticket clause. "Oct 6 incident-window DB metrics still need inspection" and "read the remainder ... from offset 121" are stale: that data is in the retained group | Re-read 9 small or already-summarized artifacts. It did not re-read the DB rows; it cited the retained ones (`[db-metrics:L132-L163]`). Then answered |

### What each final decision had

| | R1 (request 12, seq 116) | R2 (request 5, seq 54) | R3 (request 7, seq 80) |
|---|---|---|---|
| Diagnosis evidence | Summary 2 plus raw slow traces (26.3 s trace with three ~8 s timeouts), raw DB rows and header | Summary 1 plus raw payment searches. No slow-trace spans were ever read | Summary 2 plus raw slow traces, raw DB rows, raw Oct 4 metrics |
| Ticket line 20 (resolved + "mitigation before this happens again") | Raw, re-read at seq 104 | **Absent.** The summary has neither half | Raw, re-read at seq 68, and cited `[ticket:L19-L20]` in the answer |
| Runbook options | Raw (re-read seq 74, retained) | Paraphrase only ("Smallest reversible candidate to evaluate ...") | Raw (re-read seq 66) |
| Mitigation as written | "make no change now"; roll back if it recurs | "Evaluate a ... profile-only rollback" | "If the problem recurs and persists, recommend ..." |
| Diagnosis / competing verdicts | pass / pass | pass / pass | pass / pass |

## 3. Four different things that look alike

Each row is a case from the recordings, not a hypothetical one.

| Category | Definition used here | Pilot cases | Was it a decision failure? |
|---|---|---|---|
| **Missing: never gathered** | Not in the final request because the agent never read it | R2: DB spans on slow requests (`db_spans_small`). R2 never read the DB spans of a slow request | No. R2 ruled out the database from DB metrics instead (query p95 24-30 ms, pool wait 1-3 ms, latency high after CPU fell) |
| **Missing: dropped by compaction** | Read, then absent from summary and body at the decision | No diagnostic evidence. One requester qualifier: the ticket clause "We still need the cause and a mitigation before this happens again" (ticket L20). It was omitted by **5 of 5** summaries in every wording, while the other half of the same line ("normal again by 15:12 without action") was kept in 4 of 5. The pinned task still asked for a reversible mitigation in every request. R1 and R3 restored the clause by re-reading the ticket. R2 decided without it | Not established. R1 and R3 had it raw, and R3 cited it, yet all three hedged. Its absence is not needed to explain R2's hedge, and whether it contributed there is untested |
| **Retained but unused** | In the final request and relevant, but not used | R1: the Oct 4 comparison (p95 ~4.2 s under v17) was in summary 2 and in the raw alerts log, but the answer says v18's contribution cannot be quantified. R3: "latency stayed high after DB recovered" was in the raw retained rows; the answer instead ruled out the DB with span and Oct 4 evidence | R1: minor, since the answer under-uses evidence bearing on a claim it makes. R3: no, the fact was redundant |
| **Re-reading consistent with citation verification** | Reading again something that was read earlier, and citing it | 18 `reread_after_compaction` calls (R1 9, R3 9). 17 of 18 fully overlap earlier reads (the other overlaps 55 of 57 lines). All are small artifacts or metric ranges the summary already carried, and the re-read lines are then cited: 9 of 25 distinct citation ranges in R1 and 14 of 26 in R3 are not copied from the summary and fall within re-read text (for example `[release-notes:L15-L18]` against the summary's `L11-L18`). In R1 the DB rows were re-read after reading the header for the first time, to interpret values the summary had flagged as uninterpretable | No. The pattern is consistent with verifying and narrowing citations, but the recordings do not show the re-reads were necessary or cost-optimal. R2 got valid citations without any: all 16 distinct ranges in its answer were copied from its summary, and the review found all 25 citation occurrences valid |
| **Decision failure (not context)** | Adequate evidence present and read, conclusion judged weak | Mitigation phrased conditionally in 3 of 3 natural runs (owner verdict: partial). In R1 and R3 the agent cites the incident's self-resolution as the reason. F (the focused run) had the same full ticket and a byte-identical system prompt, and committed outright | Yes, by the owner's verdict. It is not shown to depend on missing evidence: R1 and R3 hedged with the relevant line present. In R2 the dropped clause was absent at the decision, and its effect there is untested. Note: "a mitigation before this happens again" can also be read as "have one ready", which is the reading R3 states. This audit does not change the verdict |

Only one genuine repeat of a tool call with its earlier result still in context occurred in the whole pilot: R2's
identical search `14:2 in:payment-client-log` (seq 32 and seq 48). It happened **before** any compaction, so it
says nothing about compaction.

One harness-level inconsistency had no observed effect. In 4 of 5 compactions the summary's inventory of
"already read" and "next steps" contradicts the retained verbatim group, because the summarizer never sees
that group. The agent was not misled in any of the four (it continued from `next_offset` and cited the retained
rows). A reviewer reading only the summary would be misled, though: it would conclude, for example, that R3 had not
inspected the incident-window DB metrics when those rows were in its context. **This is why debugging has to
inspect the complete model-visible request** (pinned instructions and task, the summary, and every retained
message), not the summary alone and not a log of tool calls.

## 4. The literal-audit false alarms and what they teach

| Indicator | Flags | False on manual reading | Why |
|---|---|---|---|
| `literal_facts_lost` at a compaction | 10 | 10 | Summaries rephrase evidence |
| Fact present in the final request but "not in answer" (`suggested_failure_mode`) | 18 | 15 | The answers paraphrase too. The 3 informative flags are R1 Oct 4 (unused), R2 Oct 4 (used as v17 log behaviour, not p95) and R3 latency-after-recovery (redundant) |
| Fact "not in final request" | 4 (R2) | 3 | Present in paraphrase. The 4th (`db_spans_small`) was never gathered, which is not a loss |
| `reread_while_present` | 4 | 3 | Continuation reads overlapping a previous read by **one** boundary line (R1 traces L376; R3 checkout and db metrics L120). The 4th is the duplicate search above |

The 10 compaction flags fall into four classes:

- **Formatting (1):** `14:18-15:05` was written `14:18–15:05` with an en dash (R1 C1).
- **Operational syntax rephrased into meaning (6):** the probes for the mitigation option (`cfgctl rollback`) and
  the safety requirements (`never waive`, `reuse_across_attempts`) look for command names and config keys.
  Summaries keep the meaning ("roll back only the payment-client runtime profile to v17", "do not disable fraud
  screening ... idempotency").
- **Change of representation (1):** the Oct 4 probes match CSV rows and log fields. The summary wrote "4197 ms at
  14:02 and remained around 4.2 s".
- **Relational facts (2):** "latency stayed high after the DB recovered" relates two series. No single string can
  carry it. The summaries stated it outright ("CPU returned to 26 at 14:41 while checkout remained slow
  afterward").

What developers can take from this:

1. **A literal probe measures representation, not information.** Summarization changes representation by design,
   so every literal "lost" flag after a compaction is expected to fire on correct summaries. Use probes only to
   choose where to read.
2. **Some facts cannot be probed at all.** Comparisons, absences and "X while Y" facts need manual reading or
   structured extraction. A probe that matches on a value and an entity (8000 near "timeout") tolerates rewording
   better than one that matches a whole line.
3. **The fact list is where the audit is blind.** The one thing every summary omitted (the ticket's request for a
   mitigation before recurrence) was not in `facts.json`, so no probe could flag it. Fact lists built around *the
   cause* miss the requester's qualifiers and constraints, which bear on the *decision*.
4. **Overlap-based re-read classifiers need a minimum overlap,** just as fact probes need tolerance for
   rewording. Otherwise paging through an artifact looks like repetition.
5. **A probe hit at the decision is not use, and a miss in the answer is not misuse.** Of 18 "present but unused"
   flags, 15 were used in paraphrase, and only one marked under-use that touches a claim.

## 5. What the pilot establishes

**Established** (3 natural runs, one model, one synthetic incident, one configuration; not a rate):

- **Diagnostic evidence was retained.** At all five boundaries, every critical fact the agent had gathered
  survived in readable form. This was checked by reading the summaries, not by probes.
- No decision defect in the three runs is shown to depend on evidence missing from the final request because
  of compaction. The one shared defect, the conditional mitigation, occurred with the relevant ticket line present
  raw in 2 of 3 runs, and cited in one of them. In R2 that line was absent at the decision, and its effect there
  is untested.
- **One requester requirement was not retained by any summary.** All 5 summaries omitted the ticket's request for
  a mitigation before recurrence, while 4 of 5 kept the other half of the same sentence, which argues for
  inaction. The pinned task still required a reversible mitigation recommendation, so this is the loss of a
  timing qualifier, not of the mitigation requirement. Re-reading restored it in two runs. This is an observed
  omission; its causal impact on the final decisions is unproven.
- Post-compaction re-reads were (almost entirely) full re-reads of small artifacts or of metric ranges the
  summary already carried, followed by citations from them. That is consistent with citation verification. It is
  not evidence of lost context, and it is not shown to be necessary or cost-optimal. Under the same prompt, R2
  re-read nothing and stayed valid, so the prompt's "artifacts can be read again" sentence does not force
  re-reading. Whether it encourages it remains untested.
- Summaries can contradict the retained verbatim context about what has been read. No harm to the agent was
  observed, but a reviewer who reads the summary without the rest of the request would be misled.

**Uncertain:**

- Whether a summary's *stance* affects the decision (section 6). All five summaries framed the mitigation
  conditionally, and all three answers hedged. That is a correlation with no contrast available.
- Whether the hedge formed in the encrypted reasoning, which cannot be audited.
- Whether ticket L20 is unambiguous. Two readings are defensible.
- Anything about other models, tasks or thresholds, failure rates, or context rot.

**Practical diagnostic guidance supported by these recordings** (turned into a step-by-step procedure with worked
examples in [`DIAGNOSTIC_GUIDE.md`](DIAGNOSTIC_GUIDE.md)):

1. **Diagnose at the decision.** Open the exact request that produced the answer (`model_request` payload). For
   each defect, sort the relevant evidence into never gathered, dropped by compaction, present but unused, or
   present and used but judged differently. In this pilot each category had a different cause and a different
   fix, and only the second one implicates compaction.
2. **Inspect the complete model-visible request.** Read the summary together with the pinned task and the
   retained messages. A summary's "unread" or "next steps" can describe only the part that was removed.
3. **Check requirement retention separately from evidence retention.** Ask what the summary dropped from the
   requester's asks and constraints, not only from the evidence, and what the pinned task still covers. Look for
   two-part statements that survive only in part.
4. **Read the summary's stance, not just its facts.** Note conclusions or recommendations the summary already
   frames as provisional or settled. Whether that framing carries forward is open, but checking it costs nothing.
5. **Do not count re-reads as failures, or assume they were needed.** Check whether the re-read content appears
   in the answer. Look for
   repeated calls while the result is still in context, and check whether they happened before any compaction.
6. **Do not judge one run on a judgment call.** The three hedges use three different wordings and justifications,
   and a single sample cannot separate a context effect from sampling variation.

## 6. The one candidate experiment (deferred)

Documented for the record only. By the owner's decision of 2026-11-14 it is deferred: nothing below is
implemented, and no paid call has been made for it.

**Research question.** Does a compaction summary's provisional framing of a pending decision carry into the final
decision when the rest of the request is held fixed? Concretely: in R2's final request, does replacing only the
summary's conditional mitigation framing with neutral wording change how often the answer commits to the rollback?

**Why the recordings cannot answer it.** All five summaries frame the mitigation conditionally ("If a mitigation is
recommended", "candidate to evaluate ... not an action taken", "Consider recommending ... subject to"), and all three
answers hedge. The only run without a summary (F) committed, but it also differs in tools, evidence selection,
length and number of steps, and it is a single sample. R2's "Evaluate a ..." echoes its summary's "candidate to
evaluate", but that is n = 1, and the encrypted reasoning item retained in that request is opaque.

**Competing explanations it would separate.**

- (a) Stance inherited from the summary.
- (b) The model's own reading of the task and the self-resolved incident.
- (c) Sampling variation: the recorded hedge is not a stable property of that context.

It would not settle whether ticket L20 is ambiguous. That is a fixture-wording question, not a model one.

**Design.** Use R2, agent request 5. It is the only final request without the raw ticket or runbook, so the
summary is the only carrier of the stance.

- **Arm A:** the recorded request, unchanged. It is the variance baseline.
- **Arm B:** byte-identical except for the summary's mitigation sentences, which keep the same facts and
  citations but drop the conditional and "evaluate" framing.
- Both arms keep the opaque reasoning item unchanged. 10 samples per arm.
- The dropped ticket clause is absent in both arms. Arm B tests the summary's stance, not the compaction loss,
  so only one variable changes.
- A sample that calls a tool ends there and is recorded as its own outcome. The investigation does not continue.
- The owner grades answers blind to the arm, with "committed" and "conditional" defined in writing beforehand.
- **Decision rule, fixed in advance:**
  - A ≥ 8/10 conditional and B ≤ 2/10: carry-over supported at this checkpoint.
  - A ≥ 8/10 and B ≥ 7/10: the summary stance is not the driver.
  - A < 8/10: the recorded hedge was not stable, and nothing can be attributed.
  - Anything else: inconclusive, stop.
- Only a large effect is detectable at this size.

**What a developer would do differently.**

- **If carry-over is supported:** keep provisional conclusions and draft recommendations out of compaction
  summaries. In this prompt, "Findings so far" and "Mark uncertain points as uncertain" invite exactly that. Add a
  stance check to post-compaction diagnosis.
- **If not supported:** compaction is cleared for this decision. Look at the task wording or the model's
  disposition instead, and leave the summary prompt as it is.
- **If the hedge is not stable:** single-run verdicts on judgment calls should not be attributed to context at all.

**Smallest implementation and cost.**

- `ctxlab live continue` already re-sends a frozen request.
- **Arm A needs configuration only:** `limits.max_agent_requests = 1` in a local config. It is counted per run, so
  the continuation stops after one response. At R2's request estimate (22,309 + 12,000 reserve, below the 37,000
  threshold) no compaction fires.
- **Arm B needs new code:** an exact-match text substitution on the summary message (refused unless the old text
  matches exactly once; the variant and its hash recorded in the manifest), with two offline tests. About 40
  lines. It is a small new capability, disclosed here.
- **Expected cost:** from R2's recorded final request (15,811 input + 1,356 output tokens at the configured
  $0.125 / $0.50 per Mtok), about $0.003 per sample, so about $0.06 for 20 samples.
- **Worst case, as the Lab budgets:** estimated input × 1.5 plus the full 12,000-token output cap is about $0.010
  per sample, so about $0.20 for 20.
- Plus the owner's blind grading of 20 short mitigation sections.
- **Risk to check on the first call:** whether the provider still accepts the recorded encrypted reasoning item.

**Recommendation and decision.** The question meets the bar, but only narrowly. It targets the one context
mechanism that fact-presence auditing cannot see. It costs little, and each outcome maps to a different action. Its
result would hold for one checkpoint of one model only. The audit recommended stopping unless the owner wants to
change or publish guidance about summary prompts. **The owner decided to stop (2026-11-14), and the experiment is
deferred.** The pilot's question (lost vs. present and misused) has a negative answer for these recordings, and
the findings in sections 2-5 need no further runs.

## Method

Everything above was derived offline from the committed files: `events.jsonl` (payloads, summaries, tool calls),
`audit.json` (classifications, probe flags), `final.md`, `review.json`, `gold/.../facts.json`, the fixture and the
frozen prompts. The checks used throwaway scripts that were not committed:

- condensed timelines per run;
- a manual search of every summary for any wording of ticket L20;
- a comparison of final-answer citation ranges with the last summary's citations and with the raw reads in the
  final request, split by tool-call classification;
- overlap sizes for every read classified as a re-read.

Each check can be repeated with `ctxlab inspect` and by reading the cited `seq` events. No recording, code, rubric,
score, review or reported result was modified. The only other changes are links to this audit from
[`README.md`](README.md) and [`PILOT_SUMMARY.md`](PILOT_SUMMARY.md), plus a short note in the summary.
