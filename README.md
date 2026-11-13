# agent-failure-lab

Small experiments for the AgentProbe series **When AI Agents Fail**. Each experiment lives in its own folder with
its own locked dependencies, fixtures, offline tests and recorded runs. Recorded runs can be replayed offline
against the harness; repeating the model inference itself is a new, paid run whose output can differ.

This is experimental research. It is not a validated benchmark, and none of it has been published or approved as
an AgentProbe Lab or article. The source is public for inspection and review.

| Experiment | Question | Status |
|---|---|---|
| [`experiments/context-retention`](experiments/context-retention/) | When an agent repeats tool calls or concludes wrongly in a long workflow, was critical evidence lost during compaction, or present but used incorrectly? | Pilot complete, reviewed and recorded: 1 focused sanity run + 3 natural runs on one model and one synthetic incident. Not a benchmark; not published |

For the context-retention pilot:

- Results and limitations: [`PILOT_SUMMARY.md`](experiments/context-retention/PILOT_SUMMARY.md)
- Diagnosing your own agent: [`DIAGNOSTIC_GUIDE.md`](experiments/context-retention/DIAGNOSTIC_GUIDE.md)
- Methodology: [`README.md`](experiments/context-retention/README.md)
- Recordings: [`runs/`](experiments/context-retention/runs/) (see [`runs/README.md`](experiments/context-retention/runs/README.md))
- Offline replay and tests: [Quick start](experiments/context-retention/README.md#quick-start-offline)

Principles shared by the experiments:

- Synthetic, coherent fixtures. No production data, internal code, identifiers or metrics.
- Gold answers and rubrics stay outside what the agent can read.
- Offline tests and replay never call a model; live runs need the reader's own key and an explicit budget.
- Mock responses test plumbing only and never appear in results. Negative and inconclusive results are reported.
