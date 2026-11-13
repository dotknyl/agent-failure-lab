"""Coherence and leak checks on the checked-in fixture, by parsing the artifacts themselves.

These checks deliberately re-derive facts from artifact text (not from generator internals), so a hand edit or a
generator bug that makes sources disagree is caught.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path

from . import fixture_gen as g
from .artifacts import ArtifactStore
from .paths import FIXTURE_DIR, GOLD_DIR

LEAK_TERMS = ["root cause", "root_cause", "rootcause", "gold", "rubric", "amplif", "expected answer",
              "correct answer", "best-supported", "competing"]
LEAK_ALLOWED = {("ticket", "competing"), ("ticket", "best-supported")}  # the ticket restates the task itself
PROFILE_TIMEOUT = {"2.13.3": 2000, "2.14.0": 8000}
PROFILE_ATTEMPTS = {"2.13.3": 2, "2.14.0": 4}


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def check_regeneration(fixture_dir: Path = FIXTURE_DIR, gold_dir: Path = GOLD_DIR) -> list[str]:
    bundle = g.build()
    problems = []
    expected = dict(bundle.files)
    expected["manifest.json"] = g._json({"fixture": g.FIXTURE_ID, "generator_seed": g.SEED,
                                         "artifacts": bundle.manifest})
    for rel, text in expected.items():
        p = fixture_dir / rel
        if not p.exists() or p.read_bytes() != text.encode("utf-8"):
            problems.append(f"fixture file {rel} differs from the generator output (run `ctxlab fixture generate`)")
    gold_expected = {"support_lines.json": g._json(bundle.gold_support), "derived.json": g._json(bundle.gold_derived),
                     "focused_excerpts.json": g._json(bundle.focused),
                     "explanation.md": g._explanation(bundle.gold_derived)}
    for rel, text in gold_expected.items():
        p = gold_dir / rel
        if not p.exists() or p.read_bytes() != text.encode("utf-8"):
            problems.append(f"gold file {rel} differs from the generator output")
    listed = {Path(e["file"]).name for e in bundle.manifest}
    present = {p.name for p in (fixture_dir / "artifacts").iterdir()} if (fixture_dir / "artifacts").exists() else set()
    if present != listed:
        problems.append(f"artifact directory has unlisted or missing files: {sorted(present ^ listed)}")
    return problems


def check_coherence(store: ArtifactStore) -> list[str]:
    problems: list[str] = []
    a = store.artifacts

    # ---- traces: structure, bounds, attempts, timeouts, idempotency
    sampled: dict[str, list[dict]] = {}
    for aid in ("traces-2026-10-04", "traces-2026-10-06"):
        for n, line in enumerate(a[aid].lines, 1):
            s = json.loads(line)
            s["_start"] = _ms(s["start"])
            s["_line"] = n
            sampled.setdefault(s["trace"], []).append(s)
    for trace, spans in sampled.items():
        roots = [s for s in spans if s["parent"] is None]
        if len(roots) != 1:
            problems.append(f"trace {trace}: {len(roots)} root spans")
            continue
        root = roots[0]
        by_id = {s["span"]: s for s in spans}
        for s in spans:
            if s["parent"] is None:
                continue
            p = by_id.get(s["parent"])
            if p is None:
                problems.append(f"trace {trace}: span {s['span']} has unknown parent")
                continue
            if s["_start"] < p["_start"] or s["_start"] + s["duration_ms"] > p["_start"] + p["duration_ms"]:
                problems.append(f"trace {trace}: span {s['name']} outside its parent {p['name']}")
        direct = [s for s in spans if s["parent"] == root["span"]]
        if sum(s["duration_ms"] for s in direct) > root["duration_ms"]:
            problems.append(f"trace {trace}: children exceed root duration")
        release = root["attrs"]["release"]
        attempts = sorted((s for s in spans if s["name"] == "payment.attempt"), key=lambda s: s["attrs"]["attempt"])
        auth = next(s for s in spans if s["name"] == "payment.authorize")
        if [s["attrs"]["attempt"] for s in attempts] != list(range(1, len(attempts) + 1)):
            problems.append(f"trace {trace}: attempt numbers not consecutive")
        if len(attempts) > PROFILE_ATTEMPTS[release] or auth["attrs"]["attempts"] != len(attempts):
            problems.append(f"trace {trace}: attempt count inconsistent with release {release}")
        for s in attempts:
            at = s["attrs"]
            if at["timeout_ms"] != PROFILE_TIMEOUT[release] or at["max_attempts"] != PROFILE_ATTEMPTS[release]:
                problems.append(f"trace {trace}: attempt attrs do not match the profile of release {release}")
            if at["outcome"] == "timeout" and not at["timeout_ms"] <= s["duration_ms"] <= at["timeout_ms"] + 10:
                problems.append(f"trace {trace}: timeout attempt lasted {s['duration_ms']} ms")
            if at["idempotency_key"] != auth["attrs"]["idempotency_key"]:
                problems.append(f"trace {trace}: idempotency key changes across attempts")
        for prev, nxt in zip(attempts, attempts[1:]):
            if prev["attrs"]["outcome"] == "ok":
                problems.append(f"trace {trace}: attempt after a successful one")
            if nxt["_start"] < prev["_start"] + prev["duration_ms"]:
                problems.append(f"trace {trace}: attempts overlap")
        failed = bool(attempts) and attempts[-1]["attrs"]["outcome"] != "ok"
        if failed != (root["attrs"]["http.status"] == 502):
            problems.append(f"trace {trace}: HTTP status inconsistent with attempt outcomes")

    # ---- payment client log joins sampled traces
    log_index: dict[tuple[str, int], dict] = {}
    pat = re.compile(r"trace=(\w+) order=\S+ attempt=(\d+)/(\d+) outcome=(\w+) elapsed_ms=(\d+) timeout_ms=(\d+)")
    for line in a["payment-client-log"].lines:
        m = pat.search(line)
        if m:
            log_index[(m.group(1), int(m.group(2)))] = {"max": int(m.group(3)), "outcome": m.group(4),
                                                        "elapsed": int(m.group(5)), "timeout": int(m.group(6))}
    for trace, spans in sampled.items():
        for s in spans:
            if s["name"] != "payment.attempt" or s["attrs"]["outcome"] == "ok":
                continue
            entry = log_index.get((trace, s["attrs"]["attempt"]))
            if entry is None:
                problems.append(f"trace {trace} attempt {s['attrs']['attempt']}: no payment-client log line")
            elif (entry["elapsed"], entry["outcome"], entry["timeout"]) != (
                    s["duration_ms"], s["attrs"]["outcome"], s["attrs"]["timeout_ms"]):
                problems.append(f"trace {trace} attempt {s['attrs']['attempt']}: log disagrees with span")

    # ---- metrics and alerts: recompute alerts from the CSV text
    rows = list(csv.DictReader(io.StringIO("\n".join(a["checkout-metrics"].lines))))
    db_rows = {r["minute_utc"]: r for r in csv.DictReader(io.StringIO("\n".join(a["db-metrics"].lines)))}
    if set(r["minute_utc"] for r in rows) != set(db_rows):
        problems.append("checkout-metrics and db-metrics cover different minutes")
    expected_alerts = []
    for day in (g.DAY_A, g.DAY_B):
        drows = [r for r in rows if r["minute_utc"].startswith(day)]
        expected_alerts += _alerts(drows, "checkout-latency-p95", lambda r: int(r["p95_ms"]) > g.LATENCY_ALERT_MS,
                                   g.LATENCY_ALERT_MINUTES)
        expected_alerts += _alerts([db_rows[r["minute_utc"]] for r in drows], "orders-db-cpu",
                                   lambda r: int(r["cpu_pct"]) > g.DB_CPU_ALERT_PCT, g.DB_CPU_ALERT_MINUTES)
    logged = {(l.split()[0], l.split()[1], l.split()[2]) for l in a["alerts-log"].lines
              if l.split()[2] in ("checkout-latency-p95", "orders-db-cpu")}
    if logged != set(expected_alerts):
        problems.append(f"alerts log disagrees with metrics: {sorted(logged ^ set(expected_alerts))}")
    for r in rows:
        release = r["release"]
        minute = _ms(r["minute_utc"])
        if minute >= _ms("2026-10-06T14:04:00Z") and release != "2.14.0":
            problems.append(f"metrics row {r['minute_utc']}: release {release} after the deploy completed")
    # Design assertions that keep the case unambiguous (checked on artifact text).
    rel_ok = [int(r["p95_ms"]) for r in rows if "2026-10-06T14:05" <= r["minute_utc"] < "2026-10-06T14:19"]
    if not rel_ok or max(rel_ok) >= 1000:
        problems.append("after release, before the gateway degradation, p95 should stay under 1000 ms")
    day_a = [int(r["p95_ms"]) for r in rows if "2026-10-04T14:03" <= r["minute_utc"] < "2026-10-04T14:24"]
    if not day_a or max(day_a) > 5000:
        problems.append("2026-10-04 degraded p95 should stay bounded (<= 5000 ms)")
    after_db = [int(r["p95_ms"]) for r in rows if "2026-10-06T14:42" <= r["minute_utc"] < "2026-10-06T15:03"]
    if not after_db or min(after_db) < 8000:
        problems.append("latency should stay high after the db maintenance ended")
    slow_db = [s["duration_ms"] for spans in sampled.values() if spans[0]["start"].startswith(g.DAY_B)
               and spans[0]["duration_ms"] > g.LATENCY_ALERT_MS for s in spans if s["service"] == "orders-db"]
    if not slow_db or max(slow_db) >= 100:
        problems.append("db spans on slow 2026-10-06 traces should be small")
    return problems


def _alerts(rows, name, breached, need):
    out, firing, streak = [], False, 0
    for r in rows:
        streak = streak + 1 if breached(r) == (not firing) else 0
        if streak >= need:
            firing = not firing
            t = _ms(r["minute_utc"]) + 60_000
            out.append((g.iso_s(t), "FIRING" if firing else "RESOLVED", name))
            streak = 0
    return out


def check_leaks(store: ArtifactStore, fixture_dir: Path = FIXTURE_DIR) -> list[str]:
    problems = []
    manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["artifacts"]:
        aid = entry["id"]
        surfaces = {"title": entry["title"], "description": entry["description"], "file": entry["file"],
                    "id": aid, "text": "\n".join(store.artifacts[aid].lines)}
        for where, text in surfaces.items():
            low = text.lower()
            for term in LEAK_TERMS:
                if term in low and (aid, term) not in LEAK_ALLOWED:
                    problems.append(f"artifact {aid} {where} contains answer-like term {term!r}")
    if (fixture_dir.resolve() in GOLD_DIR.resolve().parents) or GOLD_DIR.resolve() == fixture_dir.resolve():
        problems.append("gold directory must not be inside the fixture directory")
    for bad in ("../../gold/checkout-latency-v1/rubric", "..\\manifest", "manifest", "rubric", "derived"):
        if store.read_artifact(bad).ok:
            problems.append(f"read_artifact accepted a non-manifest id {bad!r}")
    return problems


def run_all(fixture_dir: Path = FIXTURE_DIR, gold_dir: Path = GOLD_DIR) -> list[str]:
    problems = check_regeneration(fixture_dir, gold_dir)
    store = ArtifactStore(fixture_dir)
    return problems + check_coherence(store) + check_leaks(store, fixture_dir)
