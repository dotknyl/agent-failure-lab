"""Aggregate recorded runs into ``REPORT.md`` and ``results.json``.

Only runs whose manifest says ``provenance: real`` are eligible. Mock or scripted runs are refused, so they can
never appear in an empirical table. Unreviewed criteria are shown as ``unreviewed``, not inferred.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .audit import analyze
from .paths import RUNS_DIR
from .scoring import load_rubric


def collect(runs_dir: Path = RUNS_DIR) -> tuple[list[dict], list[str]]:
    rows, skipped = [], []
    for d in sorted(p for p in Path(runs_dir).iterdir() if p.is_dir()) if Path(runs_dir).exists() else []:
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        if manifest.get("provenance") != "real":
            skipped.append(f"{d.name}: provenance {manifest.get('provenance')!r} (only 'real' runs are reported)")
            continue
        if not (d / "events.jsonl").exists() or not (d / "result.json").exists():
            skipped.append(f"{d.name}: incomplete recording")
            continue
        audit = analyze(d)
        review = json.loads((d / "review.json").read_text(encoding="utf-8")) if (d / "review.json").exists() \
            else None
        cfg = manifest["config"]
        rows.append({"run_id": manifest["run_id"], "condition": manifest["condition"],
                     "parent": manifest.get("parent"), "model": cfg["provider"]["model"],
                     "base_url": cfg["provider"]["base_url"], "stop_reason": audit["stop_reason"],
                     "compaction_tested": audit["compaction_tested"], "totals": audit["totals"],
                     "tool_call_classification": audit["tool_call_classification"],
                     "review": review, "threshold": cfg["context"]["compaction_threshold_tokens"],
                     "native": cfg["context"]["native_context_tokens"]})
    return rows, skipped


EVIDENCE_MODES = ("evidence_missing_at_decision", "evidence_present_misused")


def failure_mode_cell(review: dict | None) -> str:
    """Manual failure mode; evidence-based modes need inspected references (see review.json rule)."""
    mode = (review or {}).get("failure_mode")
    if not mode:
        return "unreviewed"
    if mode in EVIDENCE_MODES:
        refs = [e for e in (review.get("evidence_inspection") or [])
                if isinstance(e, dict) and e.get("fact") and e.get("where") and e.get("quote")]
        if not refs:
            return f"{mode} (UNSUPPORTED: no inspected references in review.json)"
    return mode


def build(runs_dir: Path = RUNS_DIR, out_dir: Path | None = None) -> str:
    rows, skipped = collect(runs_dir)
    rubric = load_rubric()
    crit_ids = [c["id"] for c in rubric["criteria"]]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Context retention Lab: results", "", f"Generated {now} from `runs/` by `ctxlab report`.", ""]
    if not rows:
        lines += ["**No real model runs are recorded yet. All findings are unmeasured.**", "",
                  "The harness, fixture, rubric and offline checks exist; empirical replay artifacts are pending an "
                  "authorized live pilot (see README, \"Pilot sequence\").", ""]
    else:
        lines += ["| Run | Condition | Model | Stop | Compactions | Agent req. | Tool calls (new / reread after "
                  "compaction / reread while present / dup) | Prompt tokens (summary) | Completion tokens | Cost "
                  "reported (upper bound) | " + " | ".join(crit_ids) + " | Failure mode |",
                  "|" + "---|" * (9 + len(crit_ids) + 1)]
        for r in rows:
            t = r["totals"] or {}
            c = r["tool_call_classification"]
            rv = r["review"] or {}
            verdicts = [((rv.get("criteria") or {}).get(cid) or {}).get("verdict") or "unreviewed" for cid in crit_ids]
            cost = t.get("cost_reported_usd")
            cost_s = "unknown (no pricing)" if cost is None else f"${cost:.4f} (${t.get('cost_upper_bound_usd'):.4f})"
            lines.append(
                f"| `{r['run_id']}` | {r['condition']}{' (from ' + r['parent']['run_id'] + ')' if r['parent'] else ''}"
                f" | {r['model']} | {r['stop_reason']} | {t.get('compactions')} | {t.get('agent_requests')} | "
                f"{c.get('new', 0)} / {c.get('reread_after_compaction', 0)} / {c.get('reread_while_present', 0)} / "
                f"{c.get('duplicate_in_turn', 0)} | {t.get('prompt_tokens')} ({t.get('summary_prompt_tokens')}) | "
                f"{t.get('completion_tokens')} | {cost_s} | " + " | ".join(verdicts)
                + f" | {failure_mode_cell(r['review'])} |")
        natural = [r for r in rows if r["condition"] == "natural"]
        untested = [r["run_id"] for r in natural if not r["compaction_tested"]]
        lines += ["", f"Natural runs: {len(natural)}; without any compaction (did not test compaction): "
                      f"{len(untested)}" + (f" ({', '.join(untested)})" if untested else "") + "."]
        thresholds = sorted({(r["threshold"], r["native"]) for r in rows})
        lines += ["", "Configured compaction threshold vs native window: "
                  + "; ".join(f"{t} / {n} tokens" for t, n in thresholds)
                  + ". A threshold below the native window is a disclosed stress setting."]
    if skipped:
        lines += ["", "Not reported:"] + [f"- {s}" for s in skipped]
    lines += ["", "Verdicts come from each run's `review.json` (manual). Indicators in `score.json` are not verdicts. "
                  "One wrong answer does not establish context rot.",
              "Focused runs have no tools and curated evidence: they only check that the task is solvable from "
              "sufficient evidence. A focused/natural difference is not evidence of context rot.",
              "All numbers in this table are measured agent outcomes from recorded runs. Latency figures about the "
              "incident itself are properties of the synthetic fixture (gold/), not results.", ""]
    text = "\n".join(lines)
    out_dir = Path(out_dir or runs_dir.parent)
    with open(out_dir / "REPORT.md", "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    with open(out_dir / "results.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"generated": now, "runs": rows, "skipped": skipped}, indent=2) + "\n")
    return text
