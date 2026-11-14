# Diagnosing context failures in an agent run

**The question:** when an agent makes a wrong or weak decision, was the evidence it needed missing, truncated,
compacted away, or present but used badly?

The method in short: **show what reached the model, test what the model did with it, and only then diagnose the
context failure.** This guide turns that into checks you can run on your own agent, whatever framework it uses.
The two worked examples come from this Lab's recorded pilot (one model, one synthetic incident, three natural
runs). They show the method. They are not evidence about how often failures happen.

The method and limits of this Lab are in [`README.md`](README.md), and what the pilot found is in
[`PILOT_SUMMARY.md`](PILOT_SUMMARY.md). Every boundary is walked through in [`PILOT_AUDIT.md`](PILOT_AUDIT.md).
The examples here were re-checked against the raw recordings ([provenance](#reproduce-the-inspections)). That
check was assistant-assisted, not an independent human review.

## 1. What to record

You can't diagnose a context failure in an agent that doesn't record its context. At minimum, capture these:

| Record | Why you need it | In this Lab (`events.jsonl`) |
|---|---|---|
| **The exact outgoing model request**, every time, logged at the last point before the provider call | It is the strongest application-side evidence of what the model was given. Logs of tool calls, memory stores or summaries are not | `model_request.payload` (as assembled) and `wire_payload` (as the client sent it) |
| **Each tool call and its full result**, joined by a correlation id | Lets you tell "never fetched" apart from "fetched, then lost" | `tool_call` / `tool_result`, joined by `call_id` |
| **The bounds of every result** (clipped, paged, a size budget) | Truncation must be visible in the result itself, not inferred later | The read header: `lines a-b of N`, `bounded_by`, `has_more` |
| **Every context transformation**: which messages were dropped, summarized or kept verbatim, by id | Lets you trace a fact from the tool result to its removal | `compaction_started` (`summarized_tool_call_ids`, `retained_tool_call_ids`) |
| **The summarizer's own input and output** | Shows whether the summarizer had the fact and dropped it | The `summary`-purpose `model_request` and `compaction_finished.summary` |
| **The model response and the decision taken**, linked to the request that produced it | You diagnose a decision, not a run | `model_response`, `final.md`, `run_finished` |
| **Anything opaque**, at least counted | Unreadable content (here, encrypted reasoning) is a blind spot you must declare | Reasoning items are recorded and counted, not readable |

Framework-independent rule: log the message list where your code hands it to the model client. A wrapper around the
client works with any framework. Logging upstream, before trimming, memory injection or summarization, records
what you *meant* to send, not what was sent.

Keep three layers apart: what your application assembled, what the client or SDK actually sent (it may convert
or add fields), and what the provider processed. The last is usually not observable: provider-side truncation,
filtering or caching does not show in your logs. The recorded request proves what was sent, not that the model
read every part of it unchanged.

**Treat these traces as sensitive.** Full requests and tool results can contain API keys, credentials, personal
data, proprietary content or another tenant's data. Store them under access control with a retention limit, and do
not log raw payloads without restriction in production. Redacting them may be necessary, but a redacted trace no
longer shows exactly what was sent and cannot be replayed byte for byte, so decide what to redact before you need
the trace.

## 2. Procedure: from the decision back to the evidence

1. **Pin the decision.** Find the response that contains the bad decision and the exact request that produced it.
   Everything after this step is about that one request.
2. **List what the decision needed, in two separate lists:**
   - *Requirements:* the pinned task, the requester's asks and qualifiers (deadlines, "before X happens"), and
     constraints.
   - *Diagnostic evidence:* the facts that support or rule out a conclusion.

   Compaction can keep all the evidence and still drop a requirement ([Example A](#example-a-a-requester-qualifier-omitted-by-compaction)).
3. **Read the whole request.** Read the pinned instructions, the summary, *and* every message kept verbatim beside
   the summary. For each item, decide whether it is there verbatim, there in paraphrase, or absent. Never decide
   from the summary alone ([Example B](#example-b-a-summary-that-contradicts-the-retained-context)).
4. **If an item is absent, trace it backward to its original source.** A requirement usually comes from a user
   message, the system or developer instructions, or a handoff from another agent, and sometimes from a tool
   result (in the pilot, a requester's line in the ticket). Diagnostic evidence usually comes from tool results.
   Find the message that first carried the item, then:
   - It never reached any message: **never collected or delivered.** For evidence, that is a search or planning
     issue. For a requirement, the instruction or handoff never passed it on. Neither is a context issue.
   - It was in a message that was clipped or paged: **truncated.** Check that message's bounds.
   - It was in a message that the transformation log shows as summarized: check the summarizer's input. If the
     item was there and the summary lacks it, **the summary dropped it.**
   - It was in a message that is neither summarized nor retained: **dropped during assembly.** That is an
     integration bug, the same class as events that never enter the session (an example is in
     [`REFERENCES.md`](REFERENCES.md)).
5. **If an item is present, check what the answer did with it:**
   - used;
   - unused but redundant (other present evidence settles the point);
   - unused and relevant to a claim the answer makes;
   - used but judged differently from what you expected.

   Only the third, and arguably the fourth, are reasoning defects, and neither is a context-delivery defect.
6. **Check repetition separately.** A repeated tool call matters only if its earlier result was still in the
   request (redundant), or if the repeat changed nothing in the answer. See [section 3](#3-pitfalls-that-cause-misdiagnosis).
7. **Only now attribute a cause,** and keep your claim at the level of what you observed: present, absent,
   dropped, used. "The omission caused the decision" needs a controlled comparison ([section 4](#4-before-you-say-context-rot)).

## 3. Pitfalls that cause misdiagnosis

- **Reading the summary as if it were the context.**
  - A summarizer usually sees only the part it removes. Its "already read" and "next steps" can contradict
    messages kept verbatim right beside it.
  - In this pilot that happened at 4 of 5 compaction boundaries ([Example B](#example-b-a-summary-that-contradicts-the-retained-context)).
    The agent was not misled, but a developer who read only the summary would be.
- **Trusting literal fact detection.**
  - Summaries reword by design, so a regex looking for the original string misses correct paraphrases.
  - In the pilot, 10 of 10 automated "fact lost at compaction" flags were false. One was an en dash written as
    `14:18–15:05` where the probe expected `14:18-15:05`. Six were command names and config keys (`cfgctl rollback`,
    `reuse_across_attempts`) rewritten as meaning.
  - Facts that compare two series ("latency stayed high after the DB recovered") cannot be found by any single
    string.
  - Use probes only to decide where to read ([audit section 4](PILOT_AUDIT.md#4-the-literal-audit-false-alarms-and-what-they-teach)).
- **A fact list that only covers the cause.** If your checklist holds only diagnostic facts, it cannot flag a
  dropped requirement. In the pilot, the one thing every summary omitted was the requester's timing qualifier,
  and it was not on the fact list.
- **Counting re-reads as forgetting.** After a compaction, an agent may legitimately re-read material that its
  summary already paraphrases. To tell the cases apart:
  - **Verification:** the re-read covers the same lines as an earlier read, and the answer then cites those
    lines. In runs 1 and 3, 17 of the 18 post-compaction re-reads fully overlapped an earlier read. 9 of 25
    (run 1) and 14 of 26 (run 3) distinct citation ranges in the answers were not copied from the summary and fall
    within re-read text.
  - **Redundant:** the identical call is repeated while its earlier result is still in the request. Run 2
    repeated `search_artifacts("14:2 in:payment-client-log")` at seq 32 and seq 48, before any compaction, so
    compaction cannot be blamed.
  - **Not repetition:** a read that overlaps an earlier one by a single boundary line is paging, not
    repetition. Three of the four "re-read while present" flags in the pilot were exactly that.
  - **The limit:** this shows the re-reads were consistent with citation verification. It does not show they were
    necessary. Run 2 re-read nothing and still produced valid citations.

## 4. Before you say "context rot"

"Context rot" means the model used present evidence worse *because of* context length or position. To claim it,
you need all four of the following:

1. **Presence:** the evidence was verbatim in the exact request (step 3), not merely collected earlier.
2. **Delivery ruled out:** nothing was clipped, dropped or summarized away (step 4).
3. **Misuse:** the answer contradicts or ignores that evidence in a way that matters (step 5).
4. **A controlled comparison:** the same content, varying only length or position, sampled more than once, gets
   the decision right more often when the context is shorter or better placed.

Without item 4 you have a single case of misuse, which could equally be sampling variation, the task wording,
or the model's judgment. The pilot met items 1 to 3 at most in minor cases and never ran item 4. **It does not
demonstrate context rot.**

## 5. Worked examples

Run directories are under [`runs/`](runs/). `seq` is the event number in that run's `events.jsonl`. Ticket lines
refer to [`fixtures/checkout-latency-v1/artifacts/ticket.md`](fixtures/checkout-latency-v1/artifacts/ticket.md).

### Example A: a requester qualifier omitted by compaction

Run 2, [`20261113T163542Z-natural-284440`](runs/20261113T163542Z-natural-284440/).

1. **The requirement.** Ticket L20: "15:12 UTC: checkout latency looks normal again without any action from us.
   We still need the cause and a mitigation before this happens again."
2. **Where the agent read it.** Agent request 2 read the whole ticket (call seq 8, result seq 9, lines 1-30). The
   line was in agent requests 3 and 4 (seq 22, 38).
3. **What compaction did.**
   - The summarizer's input (seq 51) contained ticket L20. Its output, summary 1 (seq 53), does not contain the
     sentence in any wording, and does not mention 15:12.
   - It keeps the task and constraints, citing `[ticket:L24-L30]`: "…and recommend the smallest reversible
     mitigation… Do not disable or weaken payment safety checks".
   - It also keeps the facts that the incident ended: "the provider marked the incident resolved at 15:09".
   - It frames the mitigation tentatively: "Smallest reversible candidate to evaluate is rolling back only the
     payment-client runtime profile to v17 … This is a recommendation for evaluation, not an action taken."
4. **The final request (seq 54)**, in order:
   - system prompt;
   - the pinned task, which still says "recommend the smallest reversible mitigation";
   - summary 1, delivered as a user message;
   - the most recent group, kept verbatim: one assistant turn with five searches (payment spans in both trace
     files, three payment-log searches) and their results.

   Neither half of ticket L20 is in it, and neither is the raw runbook.
5. **The decision.** In [`final.md`](runs/20261113T163542Z-natural-284440/final.md): "Evaluate a
   **payment-client profile-only rollback from v18 to v17**, rather than rolling back the entire release." This is
   the correct option, worded as something to evaluate. The owner's verdict on the mitigation is *partial*.
6. **Why this does not show that the omission caused the weaker wording:**
   - The requirement to recommend a reversible mitigation was never lost, because the pinned task carried it.
     Only the timing qualifier was lost.
   - Runs 1 and 3 had the full L20 back in their final requests, raw, through re-reading the ticket (result seq
     105 and seq 69). They hedged anyway ("make no change now"; "If the problem recurs and persists, recommend…").
     Run 3 even cited `[ticket:L19-L20]` to justify waiting. So the line's presence did not prevent the hedge.
   - Another explanation is also consistent with run 2: its answer echoes its summary's "candidate to evaluate".
     That is untested too (see the deferred experiment in the audit).
   - Each run is a single sample, and the encrypted reasoning items cannot be read.

**Observed:** the summarizer had the qualifier and omitted it, and run 2 decided without it.
**Not established:** that the omission changed the decision.
**What would establish it:** the same final request with only the qualifier restored, sampled repeatedly. This
was not run and is not planned.

### Example B: a summary that contradicts the retained context

Run 3, [`20261113T163628Z-natural-a077c6`](runs/20261113T163628Z-natural-a077c6/), compaction 2.

1. **What the summary claims.** Summary 2 (seq 55) says:
   - "Oct 6 incident-window DB metrics still need inspection."
   - "Read the remainder of checkout and DB metrics (from offset 121)".
   - Under "Artifacts already read" it lists only `db-metrics:L1-L120`.
2. **What was actually kept beside it.** The compaction (seq 52) retained the request-4 group verbatim. That
   group includes `read_artifact(db-metrics, offset 120, limit 80)` (call seq 42, result seq 43, `lines 120-199 of
   226`). Those rows are exactly the incident window:
   - L132 `2026-10-06T14:10:00Z,90,28,20,1,402` (CPU 90%, query p95 28 ms, pool wait 1 ms);
   - L163 `2026-10-06T14:41:00Z,26,18,14,1,266`.

   The summarizer's input (seq 53) held none of the six retained results. That is consistent with how the summary
   prompt works: it is given only "the earlier investigation steps that are being removed" (`SUMMARY_USER` in
   [`src/ctxlab/prompts.py`](src/ctxlab/prompts.py)).
3. **The context the agent acted in.** Agent request 5 (seq 56) is the request in which the agent chose its next
   step. It holds exactly 10 messages:
   - system prompt;
   - pinned task;
   - summary 2;
   - the retained request-4 group: one assistant turn, then six results (checkout metrics L120-199, DB metrics
     L120-199, slow traces L331-360, two payment-log searches, Oct 4 traces L20-44).

   The final request (seq 80) still contains that retained group, followed by the two groups of reads described
   below.
4. **What the agent did next.**
   - It did **not** re-read DB rows 120-199. After the compaction it re-read nine earlier ranges (seq 58-76): the
     db-metrics header (L1-8), seven small artifacts and the Oct 4 checkout rows (L32-60). It also read
     `payment-client-log` L1-8 for the first time.
   - Its answer then cites `[db-metrics:L132-L163]` for "CPU reached 80–92% during the reindex window", which
     matches the retained rows. It also cites that range for query p95 of "14–30 ms", where those rows show 18–30
     ms: a small reading slip.
   - The continuation was correct: the agent used the retained evidence and was not misled by the summary.
5. **The misdiagnosis to avoid.** A developer reading summary 2 alone would conclude that the agent "forgot" or
   never inspected the incident-window DB metrics. They would then suspect compaction of dropping them, or flag
   the agent for ruling out the database without evidence. Both conclusions would be wrong: the rows were in the
   request, verbatim.

The same pattern appears at three other boundaries, and in none of them did it mislead the agent:
- run 1 compaction 1: "`payment-client-log`, `db-metrics` … remain unread";
- run 2 compaction 1: "No content from traces-2026-10-04 was read";
- run 3 compaction 1: "Metrics, traces, payment-client log … were listed but not inspected".

## Reproduce the inspections

No API key or network is needed. The data is the committed recording. Save this as `show_context.py` outside the
repository and run it from `experiments/context-retention/` with the Lab's Python (`uv run python …`):

```python
import json, sys

sys.stdout.reconfigure(encoding="utf-8")  # summaries contain characters such as "→"

run, needle = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
ev = [json.loads(line) for line in open(f"{run}/events.jsonl", encoding="utf-8")]
first = lambda text: text.splitlines()[0][:100] if text else ""
results = {e["call_id"]: e for e in ev if e["type"] == "tool_result"}

if needle:  # every event whose recorded text contains the needle
    for e in ev:
        if needle in json.dumps(e, ensure_ascii=False):
            print(e["seq"], e["type"], e.get("purpose", ""))
    sys.exit()

for e in ev:
    if e["type"] == "compaction_started":
        print(f"compaction {e['n']} (seq {e['seq']}) keeps verbatim:")
        for call_id in e["retained_tool_call_ids"]:
            print("   ", first(results[call_id]["content"]))
    elif e["type"] == "compaction_finished":
        print(f"summary {e['n']} (seq {e['seq']}):\n{e['summary']}\n")

final = [e for e in ev if e["type"] == "model_request" and e["purpose"] == "agent"][-1]
print(f"final request (seq {final['seq']}), message by message:")
for m in final["payload"]["messages"]:
    content = m["content"] if isinstance(m.get("content"), str) else ""
    print("   ", m["role"], first(content))
```

Run it in two ways:
- `uv run python show_context.py runs/20261113T163542Z-natural-284440 "before this happens again"` lists every event that
  carries the qualifier (seq 9, 22, 38, 50, 51 in run 2, and nothing after the compaction).
- Without the second argument, it prints each compaction's retained results, each summary in full, and the final
  request message by message. For Example B's request 5, read event seq 56 of run 3 the same way: its
  `payload.messages` holds the 10 messages listed above.

Re-checked for this guide (2026-11-14, assistant-assisted), each against the raw events:

| Claim | Where to look | Result |
|---|---|---|
| No summary keeps the L20 qualifier, in any wording | Five `compaction_finished` events: R1 seq 51, 81; R2 seq 53; R3 seq 37, 55. Searched for "before this happens again", "recur", "happens again", "prevent", then read in full | Confirmed in 5 of 5. Four keep "normal again by 15:12 without action"; R2 keeps neither half |
| The qualifier is absent from R2's final request and raw in R1's and R3's | Final agent requests: R2 seq 54, R1 seq 116, R3 seq 80 | Confirmed |
| Summaries contradict retained results | `retained_tool_call_ids` at each `compaction_started` vs. the summary's inventory | Confirmed at R1 C1, R2 C1, R3 C1 and R3 C2. No contradiction at R1 C2 |
| The summarizer never sees retained results | Summary requests R1 seq 49, 79; R2 seq 51; R3 seq 35, 53 | Confirmed. R2's one apparent match is the duplicated search, also present in the summarized part |
| Duplicate vs. verification reads | Identical `tool_call` name and arguments. For each read the audit classifies as a re-read, its overlap with earlier read ranges. Each distinct final citation range sorted into: the same as a summary citation, within a range re-read after compaction, or within another raw read in the final request | One identical call (R2 seq 32/48, before compaction). 17 of 18 post-compaction re-reads fully overlap (one: 55 of 57 lines). Three "while present" flags overlap by one line. Citation ranges: R1 9 / 9 / 7 of 25, R2 16 / 0 / 0 of 16, R3 5 / 14 / 7 of 26 |
| Example B's cited rows | `orders-db-metrics.csv` L132-L163 and R3 result seq 43 | CPU 80-92% through 14:40 confirmed; query p95 18-30 ms (the answer says 14-30) |

**What remains uncertain:** whether summary framing or the omitted qualifier affects decisions, anything hidden
in the encrypted reasoning, and anything beyond this model, incident and configuration. See
[`PILOT_AUDIT.md`](PILOT_AUDIT.md#5-what-the-pilot-establishes).
