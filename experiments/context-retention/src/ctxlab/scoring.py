"""Scoring of a final report against the pre-defined rubric.

Produces ``score.json`` with deterministic indicators (sections, citation validity, cited lines that overlap
the gold support lines, regex indicators, constraint flags) and, if absent, a ``review.json`` template for the
manual verdicts. Indicators are not verdicts; ``review.json`` is authoritative and is never overwritten.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .artifacts import ArtifactStore
from .audit import analyze
from .paths import FIXTURE_DIR, GOLD_DIR
from .trace import load_manifest

CITATION = re.compile(r"(?<![\w-])([a-z][a-z0-9-]*):L(\d+)(?:\s*[-–]\s*L?(\d+))?")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$", re.MULTILINE)


def load_rubric(gold_dir: Path = GOLD_DIR) -> dict:
    return json.loads((gold_dir / "rubric.json").read_text(encoding="utf-8"))


def split_sections(text: str, names: list[str]) -> dict[str, str]:
    marks = []
    for m in HEADING.finditer(text):
        title = m.group(1).strip().lower()
        for name in names:
            if title.startswith(name.lower()):
                marks.append((m.start(), m.end(), name))
                break
    out: dict[str, str] = {}
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.setdefault(name, text[end:stop].strip())
    return out


def citations(text: str, store: ArtifactStore) -> list[dict]:
    out = []
    for m in CITATION.finditer(text):
        aid, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        art = store.artifacts.get(aid)
        valid = art is not None and 1 <= a <= b <= len(art.lines)
        out.append({"ref": m.group(0), "artifact": aid, "first": a, "last": b, "valid": valid})
    return out


def score(run_dir: Path, gold_dir: Path = GOLD_DIR, fixture_dir: Path = FIXTURE_DIR) -> dict:
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    rubric = load_rubric(gold_dir)
    support = json.loads((gold_dir / "support_lines.json").read_text(encoding="utf-8"))
    store = ArtifactStore(fixture_dir)
    audit = analyze(run_dir, gold_dir)
    final_path = run_dir / "final.md"
    final = final_path.read_text(encoding="utf-8") if final_path.exists() else None

    result: dict = {"run_id": manifest["run_id"], "provenance": manifest["provenance"],
                    "condition": manifest["condition"], "stop_reason": audit["stop_reason"],
                    "compaction_tested": audit["compaction_tested"], "final_answer_present": final is not None,
                    "indicator_semantics": "signals for the reviewer; review.json verdicts are authoritative"}
    if final is None:
        result["criteria"] = {}
        result["suggested_failure_mode"] = _suggest(audit, None)
        _write(run_dir, result, rubric)
        return result

    sections = split_sections(final, rubric["sections"])
    cites = citations(final, store)
    result["sections_present"] = [s for s in rubric["sections"] if s in sections]
    result["citations"] = {"total": len(cites), "valid": sum(c["valid"] for c in cites),
                           "invalid": [c["ref"] for c in cites if not c["valid"]]}
    crit_out = {}
    for crit in rubric["criteria"]:
        text = sections.get(crit["section"], "") if crit["section"] else final
        sec_cites = citations(text, store)
        hits = {k: bool(re.search(p, text, re.IGNORECASE)) for k, p in crit["indicators"].items()}
        support_hit = sorted({f for f in crit["support_facts"] for c in sec_cites if c["valid"]
                              and any(c["first"] <= n <= c["last"] for n in support.get(f, {}).get(c["artifact"], []))})
        crit_out[crit["id"]] = {
            "section_present": crit["section"] is None or crit["section"] in sections,
            "indicators": hits,
            "citations": len(sec_cites),
            "citations_valid": sum(c["valid"] for c in sec_cites),
            "support_facts_cited": support_hit,
            "support_facts_expected": crit["support_facts"],
        }
    result["criteria"] = crit_out
    flags = []
    for name, pattern in rubric["constraint_flags"].items():
        if name.startswith("_"):
            continue
        for m in re.finditer(pattern, final, re.IGNORECASE):
            s = max(0, final.rfind("\n", 0, m.start()) + 1)
            e = final.find("\n", m.end())
            flags.append({"flag": name, "line": final[s:e if e != -1 else len(final)].strip()[:300]})
    result["constraint_flags"] = flags
    result["suggested_failure_mode"] = _suggest(audit, crit_out)
    _write(run_dir, result, rubric)
    return result


def _suggest(audit: dict, crit: dict | None) -> dict:
    """A starting point for the reviewer, derived from the audit. Not a verdict."""
    stop = audit["stop_reason"]
    if stop in ("provider_error", "usage_unavailable", "invalid_history"):
        return {"mode": "tool_or_provider_failure", "why": f"stop reason {stop}"}
    if stop not in ("final_answer", "final_answer_truncated"):
        return {"mode": "resource_limit", "why": f"stop reason {stop}"}
    fd = audit["final_decision"]
    missing = [k for k, v in fd["facts"].items() if not v["in_request"]] if fd else []
    present_not_used = [k for k, v in fd["facts"].items() if v["in_request"] and not v["in_answer"]] if fd else []
    why = (f"facts not literally in final request: {missing or 'none'}; present in request but not literally in "
           f"answer: {present_not_used or 'none'}")
    if audit["condition"] == "natural" and not audit["compaction_tested"]:
        return {"mode": "not_testable", "why": "no compaction occurred; " + why}
    return {"mode": "review", "why": why}


def _write(run_dir: Path, result: dict, rubric: dict) -> None:
    with open(run_dir / "score.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    review = run_dir / "review.json"
    if not review.exists():
        template = {
            "reviewer": "",
            "reviewed_at": "",
            "criteria": {c["id"]: {"verdict": None, "notes": ""} for c in rubric["criteria"]},
            "verdict_values": ["pass", "partial", "fail"],
            "constraint_violation": None,
            "failure_mode": None,
            "failure_mode_values": list(k for k in rubric["failure_modes"]),
            "failure_mode_notes": "",
            "evidence_inspection": [],
            "evidence_inspection_rule": (
                "Required before failure_mode is evidence_missing_at_decision or evidence_present_misused: one "
                "entry per fact relied on, from reading the actual request/summary, not from probe results. "
                "Entry: {\"fact\": <facts.json id>, \"where\": \"agent request N\" | \"summary N\" | "
                "\"final answer\", \"event_seq\": <seq in events.jsonl>, \"quote\": <exact supporting text, or "
                "'absent after reading the whole message'>}. The report marks these modes UNSUPPORTED without it."),
            "suggested_failure_mode": result["suggested_failure_mode"],
        }
        with open(review, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(template, indent=2, ensure_ascii=False) + "\n")
