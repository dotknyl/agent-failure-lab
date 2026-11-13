# agent-failure-lab

Small, reproducible experiments for the AgentProbe series **When AI Agents Fail**. Each experiment lives in its
own folder with its own locked dependencies, fixtures, offline tests and recorded runs.

| Experiment | Question | Status |
|---|---|---|
| [`experiments/context-retention`](experiments/context-retention/) | When an agent repeats tool calls or concludes wrongly in a long workflow, was critical evidence lost during compaction, or present but used incorrectly? | Harness, fixture and offline checks complete; **no real model runs yet** |

Principles shared by the experiments:

- Synthetic, coherent fixtures. No production data, internal code, identifiers or metrics.
- Gold answers and rubrics stay outside what the agent can read.
- Offline tests and replay never call a model; live runs need the reader's own key and an explicit budget.
- Mock responses test plumbing only and never appear in results. Negative and inconclusive results are reported.
