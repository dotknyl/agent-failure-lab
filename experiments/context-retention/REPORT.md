# Context retention Lab: results

Generated 2026-11-13 16:45 UTC from `runs/` by `ctxlab report`.

| Run | Condition | Model | Stop | Compactions | Agent req. | Tool calls (new / reread after compaction / reread while present / dup) | Prompt tokens (summary) | Completion tokens | Cost reported (upper bound) | timeline | diagnosis | competing | mitigation | references | Failure mode |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `20261113T162558Z-focused-ea1d97` | focused | gpt-6-luna | final_answer | 0 | 1 | 0 / 0 / 0 / 0 | 12642 (0) | 1891 | $0.0055 ($0.0055) | pass | pass | pass | pass | pass | none |
| `20261113T163002Z-natural-8ea26b` | natural | gpt-6-luna | final_answer | 2 | 12 | 32 / 9 / 1 / 0 | 157631 (34572) | 8497 | $0.0240 ($0.0240) | pass | pass | pass | partial | pass | other |
| `20261113T163542Z-natural-284440` | natural | gpt-6-luna | final_answer | 1 | 5 | 19 / 0 / 1 / 0 | 61686 (20028) | 4456 | $0.0099 ($0.0099) | pass | pass | pass | partial | pass | other |
| `20261113T163628Z-natural-a077c6` | natural | gpt-6-luna | final_answer | 2 | 7 | 18 / 9 / 2 / 0 | 101899 (25156) | 7249 | $0.0164 ($0.0164) | pass | pass | pass | partial | pass | other |

Natural runs: 3; without any compaction (did not test compaction): 0.

Configured compaction threshold vs native window: 30000 / 922000 tokens; 37000 / 922000 tokens. A threshold below the native window is a disclosed stress setting.

Verdicts come from each run's `review.json` (manual). Indicators in `score.json` are not verdicts. One wrong answer does not establish context rot.
Focused runs have no tools and curated evidence: they only check that the task is solvable from sufficient evidence. A focused/natural difference is not evidence of context rot.
All numbers in this table are measured agent outcomes from recorded runs. Latency figures about the incident itself are properties of the synthetic fixture (gold/), not results.
