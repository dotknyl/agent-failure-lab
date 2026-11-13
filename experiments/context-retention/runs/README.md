# Recorded runs

Real runs from the pilot of 2026-11-13 (`gpt-6-luna`, OpenAI Responses API, `store=false`,
`reasoning_effort` medium). Configuration, code and fixture hashes are in each `manifest.json`; verdicts and
quoted evidence are in each `review.json`. Results and limitations: [`../PILOT_SUMMARY.md`](../PILOT_SUMMARY.md).

| Run | Condition | Stop | Compactions | Mitigation verdict |
|---|---|---|---|---|
| [`20261113T162558Z-focused-ea1d97`](20261113T162558Z-focused-ea1d97/) | focused (sanity, no tools) | `final_answer` | n/a | pass |
| [`20261113T163002Z-natural-8ea26b`](20261113T163002Z-natural-8ea26b/) | natural 1 | `final_answer` | 2 | partial |
| [`20261113T163542Z-natural-284440`](20261113T163542Z-natural-284440/) | natural 2 | `final_answer` | 1 | partial |
| [`20261113T163628Z-natural-a077c6`](20261113T163628Z-natural-a077c6/) | natural 3 | `final_answer` | 2 | partial |

All other criteria (timeline, diagnosis, competing explanation, references) are `pass` in every run.

[`_probes/`](_probes/) holds three endpoint probes: `20261113T160830Z-probe-1bacc8` (`gpt-5.6-luna`, Chat
Completions, rejected with HTTP 400), `20261113T162346Z-probe-a6b41f` and `20261113T162545Z-probe-282f42`
(`gpt-6-luna`, Responses). Their costs count toward the pilot budget; they are not results.

`ctxlab replay runs/<run-id>` re-executes a recording offline, feeding back the recorded model responses, and
passes only if every outgoing request is byte-identical. It checks the harness and the trace, not the model:
running the model again (`ctxlab live run`) is a new, paid inference whose output can differ.

Each run is a directory `runs/<UTC timestamp>-<condition>-<random>/` written by `ctxlab live run` or
`ctxlab live continue`, containing `manifest.json`, `events.jsonl`, `result.json`, `final.md` (when the model
gave a final answer) and, after review, `score.json`, `review.json` and optionally `audit.json`.

- Only runs with `"provenance": "real"` in `manifest.json` count. `ctxlab report` refuses anything else.
- Mock (scripted) runs exist only inside the test suite's temporary directories and are never written here.
- `runs/_probes/` holds endpoint compatibility probes (`provenance: probe`); they are not results.
- Failed, interrupted and limit-stopped runs are kept and reported, not deleted or rerun in place.
- Before publishing, check each recording for anything that is not synthetic fixture content. Credentials are
  never written, and the configured key is redacted if it ever appears.
