# References and provenance

Accessed 2026-11-13. Commit SHAs are the default-branch heads read through the GitHub API that day. No code was
copied from any reference; everything here is original code informed by them, so no third-party license text is
redistributed. The four repositories below are MIT-licensed.

| Reference | Inspected at | License | Paths read |
|---|---|---|---|
| chroma-core/context-rot | [`af80a08`](https://github.com/chroma-core/context-rot/tree/af80a08018f2b7257c0336d04bcd02f936088106) | MIT | `README.md`, `experiments/longmemeval/README.md`, `experiments/longmemeval/run/run_longmemeval.py`, `experiments/models/base_provider.py`, `results/gpt_4_1_longmemeval_focused_evaluated.csv` (head), repository tree |
| anthropics/claude-cookbooks | [`ce8b262`](https://github.com/anthropics/claude-cookbooks/blob/ce8b2629efe8b24048b127b4c79af1e9cbab93d4/tool_use/automatic-context-compaction.ipynb) | MIT | `tool_use/automatic-context-compaction.ipynb` (all markdown and code cells) |
| openai/openai-agents-python | [`64fbde5`](https://github.com/openai/openai-agents-python/tree/64fbde5d02b42ae5f9db668eb0184cdb3fc9d775/docs) | MIT | `docs/running_agents.md`, `docs/models/index.md`, `docs/sessions/index.md` |
| openai/codex issue #37121 | [issue](https://github.com/openai/codex/issues/37121), state **open**, labels bug/context/tool-calls/session, 5 comments | n/a | issue body and all comments |
| google/adk-python issue #1738 | [issue](https://github.com/google/adk-python/issues/1738), **closed 2025-08-06** by its author, labels core/request clarification | n/a | issue body, resolution comments, close event |

The readable companion page for the cookbook (platform.claude.com) was not fetched separately; the notebook at
the pinned commit was read directly. The `openai` Python SDK actually installed (3.50.0, see `uv.lock`) was
inspected for `chat.completions.create/stream`, `stream_options`, `max_completion_tokens`, usage in streams,
and the exception classes, instead of relying on documentation. (This inspection covered Chat Completions
only, not the Responses API methods.) That inspection showed that the
`chat.completions.stream()` helper's `get_final_completion()` raises `LengthFinishReasonError` /
`ContentFilterFinishReasonError` on those finish reasons (`openai/lib/_parsing/_completions.py`), so the Lab
accumulates raw chunks from `create(stream=True)` instead.

## What was adopted (conceptually) and what was excluded

### Chroma, Context Rot

- **Adopted:** paired conditions over the same items ("focused" input with only the relevant parts vs "full"
  input); separation of inference output from evaluation output; recording the model and the input token count.
  Here: the `focused` condition (curated excerpts, no tools) as a sanity check, continuations from the same
  frozen decision point, and `score.json`/`review.json` kept apart from `events.jsonl`.
- **Excluded:** the benchmark suites (NIAH, LongMemEval, repeated words), dataset downloads, multi-provider
  runners, LLM-judge scoring as the primary evaluation, and overwriting `ERROR` rows on rerun (their runner
  reprocesses failed rows; this Lab keeps every attempt and every failed run).

### Anthropic cookbook, automatic context compaction

- **Adopted:** compaction is scheduled only after the current turn's tool calls have results (the API cannot
  compact a conversation ending in an unanswered tool call); a structured summarization prompt (task and
  constraints, state, discoveries, next steps); counting summarization usage separately; the caution that a
  threshold set too low lets the summary itself re-trigger compaction (here: `compaction_ineffective` and
  `compaction_without_progress` stops).
- **Excluded / changed:** the customer-service ticket scenario (and its classification steps); the server-side
  `compaction` block (`compact-2026-09-04` beta of the Messages API). At the pinned commit the notebook reads
  that block's summary as text (`block.content`), so it is inspectable. It was not used because it belongs to a
  different API than the OpenAI integrations used here (Chat Completions, and later a stateless Responses
  integration, which the pilot used), and because application-managed
  summarization lets the Lab freeze the summary prompt, record the summarizer's exact input and usage as
  ordinary requests, and keep recent tool groups verbatim;
  replacing the *entire* history with the summary (here the most recent complete tool groups are kept
  verbatim); triggering on the *previous* response's `usage.input_tokens` (here the next payload is estimated
  before sending, and the estimate is logged next to provider usage).

### OpenAI Agents SDK

- **Adopted:** the explicit memory-strategy distinction. Client-managed history (`to_input_list()`, sessions)
  versus server-managed state (`conversation_id`, `previous_response_id`) in which the request carries only a
  delta, and `OpenAIResponsesCompactionSession`, which rewrites stored history via `responses.compact`. This is
  the reason the Lab owns the history locally and sends all of it on every request: originally through the
  stateless Chat Completions API, and later also through the Responses API with `store=false` and no
  `previous_response_id` (the integration the pilot used), so the recorded payload is the whole readable context.
  Also adopted: retries are opt-in and explicit; tool output is appended and the loop re-runs until a reply
  without tool calls.
- **Excluded:** the SDK itself as a dependency, sessions/persistence, handoffs and nested handoff history,
  multi-agent orchestration, Responses-API websocket transport, durable-execution integrations.

### openai/codex #37121 (motivation, not a specification)

An open user report: after a large tool output was truncated and the thread compacted, the continuation lost
access to tool state that was still recoverable from the persisted rollout and wrongly concluded input was
missing. Later comments by another user report related Windows Desktop recurrences, including a false
"truncated" classification after compaction. AgentProbe has **not** verified any of this. It motivates two
design choices: every bounded read states `bounded_by`/`next_offset` so truncation is never implicit, and
artifacts can always be re-read after compaction (the audit classifies such re-reads).

### google/adk-python #1738 (integration mistake, not a model or compaction failure)

The reporter's agent "lost context" after tool calls and looped. They later found they were not yielding
sub-agent events, so the events never entered the session. The issue was closed as resolved. It is used only as
an example of evidence that never reached the model (a handoff/integration bug, failure mode A in the brief's
taxonomy), which is exactly what the audit checks before blaming the model.

Stars, issue counts and comment counts were not used as evidence of how often these failures occur.
