# Context Retention Failure Lab: missing, truncated and ignored tool evidence

**Question.** When an agent repeats tool calls or reaches a wrong conclusion during a long workflow, was the
critical evidence lost during compaction, or was it still in the request and used incorrectly?

This Lab runs one agent through a read-only API incident investigation with automatic, model-generated
compaction, records exactly what reached the model at every step, and lets you tell those two cases apart.

> **Status: no real model runs yet. Every finding is unmeasured.** The fixture, harness, scoring and offline
> checks are complete. Recorded traces and results will be added only after an authorized live pilot (see
> [Pilot sequence](#pilot-sequence)). Nothing in this repository is a benchmark result.
>
> Two kinds of numbers appear here; do not mix them. **Fixture properties** (simulated checkout latency, attempt
> durations, alert times; `gold/.../derived.json`, `explanation.md`) describe the synthetic incident and are known
> by construction. **Agent outcomes** (task success, compactions, re-reads, tokens, cost) exist only in recorded
> live runs and are currently unmeasured.

## Three ways to use it

| You want to... | You need | What you get |
|---|---|---|
| **Read / inspect** | nothing | `REPORT.md`, recorded traces in `runs/`, summaries before/after each compaction, the audit and the rubric. *Pending: no real runs are recorded yet.* |
| **Replay / test offline** | Python 3.12 + [uv](https://docs.astral.sh/uv/) | `ctxlab replay` re-runs a recording against the same harness and requires byte-identical requests; tests validate fixture, tools, messages, compaction, termination and scoring. No API key, no network, no cost. |
| **Run live** | your own API key and an explicit budget | New runs with a real model. Results can differ from recorded ones (model version, sampling). There is no shared key or hosted endpoint. |

## Quick start (offline)

```bash
cd experiments/context-retention
uv sync --locked
uv run pytest                         # offline; network connections are blocked in tests
uv run ctxlab fixture verify          # bundle regenerates byte-identically, sources agree, no answer leaks
uv run ctxlab tools list              # the agent's tools, called by you
uv run ctxlab tools search "timeout_ms=8000 in:payment-client-log"
uv run ctxlab tools read config-diff --offset 25 --limit 20
uv run ctxlab report                  # aggregates real runs only (currently: none)
```

For a recorded run `runs/<run-id>/`:

```bash
uv run ctxlab inspect runs/<run-id>   # requests, estimates vs usage, compactions, fact audit, re-read classes
uv run ctxlab replay  runs/<run-id>   # offline re-execution; fails on the first differing payload
uv run ctxlab score   runs/<run-id>   # rubric indicators + review.json template for manual verdicts
```

## The incident (what the agent sees)

The agent receives this task, verbatim, and nothing about the answer:

> Investigate why checkout requests became slow after the latest release. Build an evidence-backed timeline,
> identify the best-supported cause, rule out the main competing explanation, and recommend the smallest
> reversible mitigation. Do not modify production settings or disable payment safety checks.

The evidence bundle (`fixtures/checkout-latency-v1/artifacts/`, ~567K characters) is synthetic but internally
coherent. It is produced by one seeded simulation of ~2,700 checkout requests over two days
(`src/ctxlab/fixture_gen.py`):

| Artifact id | Content |
|---|---|
| `ticket` | incident ticket, symptoms, updates, constraints |
| `release-notes` | v2.13.3 and v2.14.0 notes and deployment record |
| `config-diff` | unified diff of the deployed configuration |
| `checkout-metrics` | per-minute requests, 5xx, p50/p95/p99 for both days |
| `traces-2026-10-04`, `traces-2026-10-06` | head-sampled traces (1 in 12), one span per line |
| `payment-client-log` | all non-ok payment attempts, exhausted authorizations, config reports |
| `provider-status` | the payment provider's status history |
| `db-metrics`, `db-change-log` | orders-db metrics and change records |
| `alerts-log` | alert state changes, derived from the metrics |
| `runbook-payments` | safety requirements and mitigation options |

Several sources are needed: no single file answers the task. Logs and traces join on trace ids, attempt numbers
and durations. Alerts are recomputed from the metrics in the checks. The gold explanation, rubric, critical facts,
supporting line numbers and the focused-excerpt list live in `gold/checkout-latency-v1/`, outside the tool root.
Tools look artifacts up only by manifest id, and tests check for answer-like terms in every artifact, title,
description and file name. *Spoiler for humans:* `gold/checkout-latency-v1/explanation.md`.

## Tools (the only three)

| Tool | Semantics |
|---|---|
| `list_artifacts()` | ids, titles, kinds, sizes, neutral descriptions |
| `search_artifacts(query)` | case-insensitive AND of terms, `"quoted phrases"`, optional `in:<artifact-id>`; reports total matches per artifact, shows at most 40 lines (each clipped at 300 chars, marked) |
| `read_artifact(id, offset, limit)` | `offset` = **1-based line number** (default 1); `limit` = lines (default 100, max 200, a larger value is applied as 200 and reported); at most 16,000 characters per read |

Every read header states `lines a-b of N`, `bounded_by=end_of_artifact|limit|char_budget`, `has_more` and
`next_offset`. A line longer than 4,000 characters would be clipped with an explicit marker (none exist in the
fixture). There is no silent truncation, and any artifact can be re-read after compaction. Citations use the
same numbers: `[artifact-id:L12-L18]`.

## The loop and automatic compaction

```
pinned:  system instructions + original task (never compacted)
summary: latest compaction summary (folds in the previous one), if any
body:    complete groups = assistant message + one tool result per tool call

before every agent request:
  estimate(instructions + tool schemas + history) + output reserve >= threshold ?
    -> summarize the older complete groups with a real model call (frozen prompt, no gold)
    -> keep the most recent complete groups verbatim (keep_recent_groups, retain_budget_tokens)
    -> re-estimate; still over the threshold -> stop "compaction_ineffective"
  validate the message sequence -> send -> append the reply and every tool result
stop on a reply without tool calls, or on a named limit/error
```

- **Provider:** one integration, OpenAI **Chat Completions** via the `openai` SDK with a configurable base
  URL. It is stateless: the recorded payload *is* the model's context. Server-managed state
  (`previous_response_id`, conversations) was not used because the request then carries only a delta.
- **Why application-managed summarization rather than a hosted compaction feature:** hosted compaction is tied
  to a specific API (in the Anthropic cookbook at the pinned commit, a beta of the Messages API whose `compaction`
  block does carry readable summary text; OpenAI's `responses.compact` belongs to the Responses API and was not
  verified here). Neither is part of Chat Completions, the single integration chosen. Doing it in the
  application also lets the Lab freeze and hash the summarization prompt, record the summarizer's exact input
  transcript and output as ordinary requests with their own usage, trigger from a pre-send estimate, and keep the
  most recent tool groups verbatim. It is a design choice for observability and control, not a claim that
  hosted summaries are unreadable.
- **Retries:** SDK retries are disabled (`max_retries=0`); the runner retries explicitly (`provider.max_retries`,
  default 1, only on timeouts, connection errors and 408/409/429/5xx) and records each attempt. Streaming is a
  config flag (`stream_options.include_usage`).
- **Why not an agent framework:** the behaviour under study (what is sent, when and how it is compacted) is
  the code here, in two small modules (`runner.py`, `history.py`). A framework would hide it.
- **Token estimate:** a character heuristic (`chars_per_token`, default 3.0), with no tokenizer download. Every
  request logs the estimate next to the provider-reported `prompt_tokens`. It is an estimate, not usage.
- **Native window vs threshold:** both are recorded in every manifest. A threshold below the native window is a
  disclosed **stress setting**, not the model's limit.
- **Named stops:** `final_answer`, `final_answer_truncated`, `empty_response`, `max_agent_requests`,
  `max_tool_calls`, `max_compactions`, `compaction_without_progress`, `compaction_nothing_to_summarize`,
  `compaction_ineffective`, `compaction_input_too_large`, `compaction_failed`, `request_too_large`,
  `invalid_history`, `budget_tokens`, `budget_cost`, `provider_error`, `usage_unavailable`.

## What is recorded

`runs/<run-id>/`:

- `manifest.json`: provenance (`real` | `mock`), condition, fixture and artifact hashes, prompt hashes, tool
  schema hash, **code hashes** (`src/ctxlab/*.py`, `pyproject.toml`, `uv.lock`, git HEAD if any), config hash
  and full config (the key's environment variable *name* only), the pilot budget ledger at start, Python/SDK
  versions, disclosures. A run is traceable to its exact code even before anything is committed.
- `events.jsonl`, in order:
  - `run_started`
  - `model_request` (the exact outgoing payload, the estimate, purpose `agent`/`summary`, the attempt number)
  - `model_response` (normalized + raw body, usage, duration)
  - `model_error`
  - `tool_call` / `tool_result` (the full text the model received + bounds metadata)
  - `compaction_started` (context before, what is summarized and retained) and `compaction_finished` (summary
    text, context after, estimate after)
  - `run_finished` (stop reason, totals, final messages)
- `final.md` and `result.json`. After review: `score.json`, `review.json`, optionally `audit.json`.

No HTTP headers or credentials are recorded. The configured key is redacted if it ever appears in an event, and
the redaction is counted. **Not observable:** anything the provider does after receiving the payload (hidden
system prompts, truncation, caching, routing).

Totals include summary calls, failed attempts (as an upper bound, because billing of failed attempts is
unknown), cached and reasoning tokens. Cost is reported as provider usage × configured prices, plus an upper
bound. Without pricing it is reported as unknown.

## Scoring and diagnosis

The rubric (`gold/.../rubric.json`) and critical facts (`gold/.../facts.json`) were defined before any run.

1. **Supported timeline**
2. **Diagnosis supported by traces and configuration** (not timing alone)
3. **Competing explanation meaningfully checked**
4. **Mitigation addresses the evidence and respects constraints**
5. **References support the claims**

`ctxlab score` computes **indicators only**: sections present, citation validity, cited lines that overlap gold
supporting lines, regex indicators, and constraint flags (a sentence like "do not disable fraud screening" also
matches a flag). Verdicts are manual, in `review.json`, which is never overwritten. No LLM judge is used. String
matching is not semantic correctness.

`ctxlab inspect` gives the diagnosis inputs:

- **Fact presence per request:** in the pinned task, the summary or the body. This is a literal probe: a miss is
  not proof of absence (a paraphrase can carry the fact), and a hit is not proof of use.
- **Per compaction:** which facts were literally present before, in the summary, and after
  (`literal_facts_lost`).
- **At the final decision:** what was in the request that produced the answer, and what the answer mentions.
- **Tool calls:** `new`, `reread_after_compaction` (the earlier result was compacted away: a justified-recovery
  candidate), `reread_while_present` (the earlier result was still in context: possible repetition without
  progress), and `duplicate_in_turn`.

Failure modes a reviewer assigns:

- `evidence_missing_at_decision`
- `evidence_present_misused`
- `tool_or_provider_failure`
- `resource_limit`
- `not_testable` (for example, no compaction happened)
- `other`
- `none`

**Manual inspection is required before an evidence label.** `inspect` probe results only say where to look.
Before setting `failure_mode` to `evidence_missing_at_decision` or `evidence_present_misused`, read the actual
request and summary text in `events.jsonl` and add one `evidence_inspection` entry per fact relied on (fact id,
`agent request N` / `summary N` / `final answer`, event `seq`, and the exact supporting text, or "absent after
reading the whole message"). The report shows such a label as **UNSUPPORTED** until these entries exist.

**One wrong answer does not establish context rot**, and a repeated read is not automatically a failure.

**Focused vs natural.** The focused run has no tools and curated evidence. It only checks that the model can
solve the incident from sufficient evidence. A difference between a focused and a natural run is not evidence of
context rot: the conditions differ in tools, evidence selection, length and number of steps.

## Pilot sequence

Not run yet. Requires the owner's approval of a model and a total budget. The probe, every run, every summary
call and every retry draw on **one** pilot budget (`budget.pilot_budget_usd`; see the ledger below).

0. `uv run ctxlab fixture verify && uv run pytest` (offline; passing; also in CI), then `ctxlab preflight`.
1. **Endpoint probe:** `ctxlab live probe`. Two small requests: tool call returned, tool result accepted, usage
   reported (also when streaming). Stop if it fails.
2. **One focused sanity run:** `ctxlab live run --condition focused`, then `ctxlab score` and a manual
   `review.json`. Also read the estimate/`prompt_tokens` ratio of its single request (calibration, below). If the
   answer is wrong, first check for fixture ambiguity, missing evidence in the excerpts, and output truncation
   (`final_answer_truncated`, `finish_reason`). Never change the gold answer or rubric just to make the model pass.
3. **One natural investigation:** `ctxlab live run --condition natural`.
4. **Inspect it:** `ctxlab inspect` (requests, estimates vs usage, each summary before/after, re-reads), the
   termination reason, `final.md`, cost; then `ctxlab score` and the manual review.
5. **If that run is operationally valid** (no harness or provider failure, a meaningful stop reason) **and
   budget remains, run two more natural investigations with the identical configuration.**

If an agent solves the task before any compaction, keep the run and report that compaction was not exercised;
do not weaken search, raise read costs or force calls. `ctxlab live continue --from runs/<id> --at-request N`
(same frozen request) is reserved for a later, finding-driven comparison; it is not part of this pilot.

## Live runs: configuration and cost controls

```bash
cp configs/live.example.toml configs/live.local.toml      # *.local.toml is git-ignored
# fill model, native_context_tokens, compaction_threshold_tokens, prices + pricing_source/date,
# pilot_budget_usd + max_cost_usd, authorized_by (the owner's approval and its date)
export OPENAI_API_KEY=...                                 # or the variable named in api_key_env
uv run ctxlab preflight --config configs/live.local.toml  # no network; prints bounds; must pass
uv run ctxlab live probe --config configs/live.local.toml --confirm-spend
uv run ctxlab live run --condition focused --config configs/live.local.toml --confirm-spend
uv run ctxlab live run --condition natural --config configs/live.local.toml --confirm-spend
```

- Only `ctxlab live ...` can contact a model, and only with a passing preflight **and** `--confirm-spend`.
  Tests, replay, imports and CI cannot: the OpenAI SDK is imported lazily, and tests block sockets.
- Before **every** request, including summaries and retries, the runner checks the worst case of that request
  (estimated input × 1.5 + full output) against `max_total_tokens` and, when priced, the effective dollar cap.
  Failed attempts are charged to the upper bound.
- **Pilot ledger:** the effective cap of each invocation is `min(max_cost_usd, pilot_budget_usd - spent)`, where
  *spent* is the cost upper bound of every recorded real run and probe in `runs/` (failed and limit-stopped
  runs included). A recorded run without `result.json` (interrupted) makes the remainder unknowable, and live
  commands refuse until it is resolved. `ctxlab preflight` prints the ledger.
- The dollar cap is only as good as the configured prices and the provider's usage report. If usage is missing,
  the run stops (`usage_unavailable`) instead of guessing.
- A key in the environment is not spending authorization. `budget.authorized_by` records the owner's approval of
  the model and budget (who and when); preflight requires it.
- Output caps (`max_output_tokens` 4000, `summary_max_output_tokens` 2000 by default) may be too low for
  reasoning models, whose hidden reasoning counts against the same limit. Settle them from the probe and the
  sanity run; truncation is recorded (`final_answer_truncated`, `summary_truncated`), never hidden.

## Choosing the compaction threshold

The trigger is `estimated payload + max_output_tokens >= compaction_threshold_tokens`. Think of it as an
**input trigger** (history size at which compaction fires) plus the output reserve:
`compaction_threshold_tokens = input_trigger + max_output_tokens`. It is separate from the model's native window,
which is recorded alongside.

Measured on this fixture (estimated tokens at 3 chars/token):

| Quantity | Estimate |
|---|---|
| Pinned system prompt + task + tool schemas | ~0.9K |
| Small artifacts (ticket, notes, diff, alerts, status, change log, runbook) | 0.2-0.6K each |
| Metrics files, whole | 2.7-3.5K |
| One 200-line page of traces or the payment log (16K-char read budget) | ~5.4K |
| Traces/log artifacts, whole | 40-88K |
| History after a lean but thorough 15-step path (small artifacts, 5 searches, 2 trace pages) | ~23K |
| Context right after a compaction (pinned + summary <= ~1.8K + retained <= 6K) | <= ~9K |

Any long-context model's native window is far above the ~23-45K an investigation needs, so natural window
pressure will not occur, and compaction is only exercised with a **reduced trigger, which is a disclosed stress
setting**. Recommended input trigger: **18K estimated tokens**:

- at least 2x the post-compaction floor (~9K), so each compaction frees room for about 1.5 more trace pages
  instead of compacting every turn (`compaction_ineffective` guards the degenerate case);
- below the ~23K that even a lean thorough path reaches, so a typical natural run compacts at least once; a run
  that does not is reported as "compaction not exercised";
- it bounds per-request input to ~18K (+ output), which keeps per-run cost predictable: roughly 0.15-0.5M input
  tokens for a natural run of 15-30 requests, plus summary calls.

So `compaction_threshold_tokens = 18000 + max_output_tokens`, with `retain_budget_tokens = 6000` and
`keep_recent_groups = 2`. **Calibration:** the focused run's single request reports the estimate next to the
provider's `prompt_tokens`. If the ratio is outside 0.8-1.25, set `chars_per_token = 3.0 × estimate /
prompt_tokens` before the first natural run, and keep everything fixed for all natural runs.

## Limitations

- One synthetic incident, one fixture version. Nothing here generalizes to other tasks or models by itself.
- Token counts before sending are heuristic; provider usage is authoritative and recorded.
- Fact probes are literal. Paraphrased facts in summaries need manual review.
- Provider-side context handling is invisible. An "OpenAI-compatible" endpoint may differ in tool calling,
  usage reporting or streaming; `ctxlab live probe` checks the minimum.
- Model outputs are stochastic. Reruns are not expected to match bit for bit; `replay` checks the harness, not
  the model.
- The focused-evidence condition's excerpt list encodes relevance (it lives with gold), and it has no tools. It
  is a sanity check, not a comparison condition; focused/natural differences do not show context rot.

## Design decisions that differ from, or sharpen, the research brief (Revision 4)

- **Provider:** OpenAI's official API (`https://api.openai.com/v1`) for every recorded run, so results are
  not affected by an intermediary. The base URL is configurable for readers, but other endpoints are not
  assumed to behave the same; `live probe` checks tool calls, usage and (for the Responses integration)
  encrypted reasoning before any run.
- **Compaction trigger:** estimate-before-send instead of the previous response's reported input tokens, so the
  next request never exceeds the threshold unnoticed. Recent complete groups are kept verbatim instead of
  replacing the whole history.
- **Focused-evidence sanity condition** has no tools (one request), to isolate "can the model reason over
  sufficient evidence" from search behaviour. Its excerpt list is gold-adjacent and disclosed.
- **Continuation variants** (retain/re-read evidence, focused vs long at the same point) are a single
  `live continue` entry point from a frozen recorded request. Specific variants are deferred until a pilot
  finding justifies one.
- **The frozen agent prompt says that earlier context may be replaced by a summary and that artifacts can be
  read again.** Real harnesses usually say this, but it nudges the re-read behaviour the audit counts. Read
  `reread_after_compaction` numbers with that in mind. The prompts are **frozen as v1 for the pilot**: their
  hashes are pinned in `tests/test_pilot_readiness.py` and recorded in every manifest; any change needs a new
  version. No separate no-nudge branch is planned.
- **Streaming** is accumulated from raw chunks (`create(stream=True)`), not through the SDK's parsing stream
  helper. That helper raises on `finish_reason` `length`/`content_filter`, which would turn a truncated answer
  or summary into a provider error and lose its text.
- **Second integration: Responses API, for tools + reasoning.** On 2026-11-13 the probe got HTTP 400 from
  `/v1/chat/completions` ("Function tools with reasoning_effort are not supported for gpt-5.6-luna ... use
  /v1/responses or set reasoning_effort to 'none'"), and OpenAI's GPT-6 guide says Chat Completions does not
  support function calling with GPT-6.1 Sol. The owner chose reasoning, so `provider.integration =
  "openai-responses"` exists. It is minimal and stateless: `store=false`, the full history on every request, no
  `previous_response_id`, non-streamed. The internal history, tools, compaction, accounting and replay are the
  same as for Chat Completions. Each `model_request` event records both the internal payload and the exact
  `wire_payload` sent.
- **Encrypted reasoning is carried, as OpenAI recommends for tool-call continuations.** With
  `include=["reasoning.encrypted_content"]`, each response's reasoning items are attached to that turn's
  assistant message and sent back unchanged with its function calls. They are recorded as **opaque data**:
  never decoded, never interpreted, not shown to the summarizer (its transcript notes "N encrypted reasoning
  item(s): opaque"), and dropped together with their group when compaction summarizes it. Retained groups keep
  theirs. The audit counts them per request. Consequences:
  - Part of each request is not auditable. Every fact probe, citation check and manual inspection applies only
    to the readable inputs (task, summary, tool calls and results), which remain complete.
  - Their size enters the token estimate as the provider-reported reasoning tokens of the response that produced
    them.
  - Reasoning is never carried between runs: each run starts from the pinned system prompt and task.
    (`live continue` replays a recorded request exactly, including any reasoning items in it; it is not part of
    the pilot.)
- **Pilot model:** `gpt-6-luna` on OpenAI's API, Responses integration, `reasoning_effort = "medium"`.
  `ctxlab live probe` (passed 2026-11-13) requires encrypted reasoning to come back and a follow-up request
  carrying it to be accepted. The model does not always reason on a trivial step (`reasoning_tokens` 0, no
  reasoning item), which the probe allows.
- **Fixture realism vs signal:** a few isolated single-request timeouts outside the degradation windows are kept
  as realistic noise and listed in the gold explanation so the rubric does not penalize mentioning them.

## Layout

```
configs/live.example.toml          live-run template (every spending field must be filled)
fixtures/checkout-latency-v1/      manifest.json + artifacts/ (the agent's only world)
gold/checkout-latency-v1/          rubric, facts, support lines, derived numbers, explanation, focused excerpts
src/ctxlab/                        fixture_gen, artifacts (tools), history, runner, client, trace, audit, scoring,
                                   report, experiment, probe, config, cli
tests/                             offline tests (mocks live here only)
runs/                              recorded runs (real runs only are reported)
REFERENCES.md                      inspected references, commits, licenses, what was adopted or excluded
```
