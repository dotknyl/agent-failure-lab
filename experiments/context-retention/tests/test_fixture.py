"""Fixture coherence, determinism, gold isolation and answer-leak checks, including proof the checks can fail."""

import hashlib
import json
import shutil

from ctxlab import fixture_checks, fixture_gen
from ctxlab.artifacts import ArtifactStore
from ctxlab.paths import FIXTURE_DIR, GOLD_DIR


def test_checked_in_fixture_passes_all_checks():
    assert fixture_checks.run_all() == []


def test_generator_is_deterministic():
    a, b = fixture_gen.build(), fixture_gen.build()
    assert a.files == b.files and a.gold_derived == b.gold_derived


def _copy_fixture(tmp_path):
    dst = tmp_path / "fx"
    shutil.copytree(FIXTURE_DIR, dst)
    return dst


def _mutate(fx, artifact_file, old, new, count=1):
    p = fx / "artifacts" / artifact_file
    text = p.read_text(encoding="utf-8")
    assert old in text
    text = text.replace(old, new, count)
    p.write_bytes(text.encode("utf-8"))
    manifest = json.loads((fx / "manifest.json").read_text(encoding="utf-8"))
    for e in manifest["artifacts"]:
        if e["file"].endswith(artifact_file):
            e["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (fx / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_coherence_check_catches_span_log_disagreement(tmp_path):
    fx = _copy_fixture(tmp_path)
    # Change one timed-out attempt's duration in the traces only: log and span now disagree.
    text = (fx / "artifacts" / "traces-2026-10-06.jsonl").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if '"outcome":"timeout"' in l)
    _mutate(fx, "traces-2026-10-06.jsonl", line, line.replace('"duration_ms":800', '"duration_ms":700'))
    problems = fixture_checks.check_coherence(ArtifactStore(fx))
    assert any("log disagrees" in p or "timeout attempt lasted" in p for p in problems)


def test_coherence_check_catches_alert_metric_disagreement(tmp_path):
    fx = _copy_fixture(tmp_path)
    _mutate(fx, "alerts.log", "2026-10-06T14:22:00Z FIRING", "2026-10-06T14:12:00Z FIRING")
    assert any("alerts log disagrees" in p for p in fixture_checks.check_coherence(ArtifactStore(fx)))


def test_leak_check_catches_answer_terms(tmp_path):
    fx = _copy_fixture(tmp_path)
    _mutate(fx, "db-change-log.md", "# orders-db change log", "# orders-db change log (not the root cause)")
    assert any("root cause" in p for p in fixture_checks.check_leaks(ArtifactStore(fx), fx))


def test_hash_mismatch_is_refused(tmp_path):
    fx = _copy_fixture(tmp_path)
    p = fx / "artifacts" / "ticket.md"
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))  # what a CRLF checkout would do
    try:
        ArtifactStore(fx)
    except Exception as e:
        assert "hash mismatch" in str(e)
    else:
        raise AssertionError("CRLF-modified artifact was accepted")


def test_gold_is_outside_the_tool_root_and_not_in_any_artifact():
    store = ArtifactStore(FIXTURE_DIR)
    assert FIXTURE_DIR.resolve() not in GOLD_DIR.resolve().parents
    gold_files = {p.name for p in GOLD_DIR.iterdir()}
    listed = store.list_artifacts().text
    for name in gold_files:
        assert name not in listed
    for aid in ("rubric", "facts", "derived", "explanation", "../gold", "manifest"):
        assert not store.read_artifact(aid).ok


def test_rubric_support_facts_exist_in_support_lines():
    rubric = json.loads((GOLD_DIR / "rubric.json").read_text(encoding="utf-8"))
    support = json.loads((GOLD_DIR / "support_lines.json").read_text(encoding="utf-8"))
    for crit in rubric["criteria"]:
        for fact in crit["support_facts"]:
            assert fact in support, (crit["id"], fact)


def test_fact_probes_hit_the_artifacts_they_describe():
    """Every probe set must match somewhere in the bundle, or the audit could never see the fact."""
    from ctxlab.audit import facts_in, load_facts
    store = ArtifactStore(FIXTURE_DIR)
    everything = "\n".join("\n".join(a.lines) for a in store.artifacts.values())
    hits = facts_in(everything, load_facts())
    assert all(hits.values()), [k for k, v in hits.items() if not v]


def test_fact_probes_match_their_own_support_lines():
    """For every fact and every artifact the generator tagged as supporting it, at least one probe must match at
    least one tagged line. Otherwise reading that evidence would show up as 'not found' in the audit."""
    from ctxlab.audit import load_facts
    store = ArtifactStore(FIXTURE_DIR)
    support = json.loads((GOLD_DIR / "support_lines.json").read_text(encoding="utf-8"))
    misses = []
    for f in load_facts():
        assert f["id"] in support, f["id"]
        for aid, lines in support[f["id"]].items():
            if not any(r.search(store.artifacts[aid].lines[n - 1]) for n in lines for r in f["_re"]):
                misses.append((f["id"], aid))
    assert misses == []


def test_deploy_time_probe_does_not_fire_on_unrelated_timestamps():
    from ctxlab.audit import load_facts
    store = ArtifactStore(FIXTURE_DIR)
    support = json.loads((GOLD_DIR / "support_lines.json").read_text(encoding="utf-8"))["deploy_time"]
    fact = next(f for f in load_facts() if f["id"] == "deploy_time")
    stray = [(aid, n) for aid, a in store.artifacts.items() for n, line in enumerate(a.lines, 1)
             if any(r.search(line) for r in fact["_re"]) and n not in support.get(aid, [])]
    assert stray == []
    assert any(r.search("profile v18 was loaded at 14:04 UTC") for r in fact["_re"])  # summary paraphrase


def test_gold_derived_numbers_support_the_intended_case():
    d = json.loads((GOLD_DIR / "derived.json").read_text(encoding="utf-8"))
    p = d["p95_ms"]
    assert p["2026-10-06 after release, gateway healthy"]["max"] < 1000  # release alone: fine
    assert p["2026-10-04 gateway degraded (profile v17)"]["max"] < 5000  # upstream alone: bounded
    assert p["2026-10-06 gateway degraded"]["median"] > 8000  # both: amplified
    assert p["2026-10-06 gateway degraded, after db maintenance"]["min"] > 8000  # db ended, latency stayed
    assert d["slow_requests_2026_10_06"]["max_db_ms"] < 100
