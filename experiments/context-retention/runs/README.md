# Recorded runs

**No real model runs are recorded yet.** Empirical traces and results are pending an authorized live pilot.

Each run is a directory `runs/<UTC timestamp>-<condition>-<random>/` written by `ctxlab live run` or
`ctxlab live continue`, containing `manifest.json`, `events.jsonl`, `result.json`, `final.md` (when the model
gave a final answer) and, after review, `score.json`, `review.json` and optionally `audit.json`.

- Only runs with `"provenance": "real"` in `manifest.json` count. `ctxlab report` refuses anything else.
- Mock (scripted) runs exist only inside the test suite's temporary directories and are never written here.
- `runs/_probes/` holds endpoint compatibility probes (`provenance: probe`); they are not results.
- Failed, interrupted and limit-stopped runs are kept and reported, not deleted or rerun in place.
- Before publishing, check each recording for anything that is not synthetic fixture content. Credentials are
  never written, and the configured key is redacted if it ever appears.
