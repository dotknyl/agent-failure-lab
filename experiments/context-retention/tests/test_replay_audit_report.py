"""Offline replay, evidence audit, scoring and report provenance gates (MOCK runs only)."""

import json

from conftest import ScriptedClient, final, summary, tool_calls
from ctxlab.audit import analyze, render
from ctxlab.experiment import continuation_state, replay, start_run
from ctxlab.report import build
from ctxlab.scoring import score

TRACES = "traces-2026-10-06"


def read(aid, offset=1, limit=200):
    return ("read_artifact", {"id": aid, "offset": offset, "limit": limit})


ANSWER = """## Timeline
- 14:04 profile v18 loaded [release-notes:L14] [config-diff:L29-L42]
- 14:19 gateway timeouts start; 14:22 latency alert; 14:41 reindex done; 15:04 recovery [alerts-log:L5-L10]
## Best-supported cause
Gateway degradation amplified by the v18 change: timeout 8000 ms and 4 attempts [config-diff:L31-L41].
On 10-04 with v17 the same degradation stayed bounded [provider-status:L13].
## Competing explanation
The orders-db reindex: db spans stay small and latency stayed high after the maintenance [db-change-log:L7].
## Recommended mitigation
Recommend cfgctl rollback of the payment_client profile to v17 (reversible) [runbook-payments:L14]. Do not disable fraud screening.
## Uncertainty
Sampled traces only [nonexistent:L1] [ticket:L9999].
"""


def mock_run(cfg, tmp_path, steps):
    return start_run(cfg, ScriptedClient(steps), "natural", "mock", runs_dir=tmp_path)


def compaction_steps(summary_text):
    return [tool_calls(read("config-diff")), tool_calls(read(TRACES)), tool_calls(read(TRACES, 60)),
            summary(summary_text), tool_calls(read("config-diff", 25, 20)), final(ANSWER)]


def test_replay_reproduces_payloads_and_detects_tampering(cfg, tmp_path):
    run_dir, _ = mock_run(cfg, tmp_path, compaction_steps("S"))
    out = replay(run_dir)
    assert out["ok"], out
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(events):
        e = json.loads(line)
        if e["type"] == "tool_result":
            e["content"] = e["content"].replace("8000", "9000")
            events[i] = json.dumps(e)
            break
    (run_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
    bad = replay(run_dir)
    assert not bad["ok"]


def test_audit_detects_literal_loss_and_rereads(cfg, tmp_path):
    run_dir, _ = mock_run(cfg, tmp_path, compaction_steps("Read the config diff and some traces; nothing found."))
    a = analyze(run_dir)
    assert a["compaction_tested"]
    comp = a["compactions"][0]
    # The config diff was summarized away, the summary does not carry its values, and the retained traces
    # (lines 60-115, before the release) only show timeout_ms 2000: the literal fact is lost...
    assert comp["facts"]["config_change"]["before"] and not comp["facts"]["config_change"]["in_summary"]
    assert "config_change" in comp["literal_facts_lost"]
    # ...and recovered by re-reading the artifact, which the audit classifies as such.
    assert a["tool_call_classification"]["reread_after_compaction"] == 1
    assert a["final_decision"]["facts"]["config_change"]["in_request"] == ["body"]
    text = render(a)
    assert "reread_after_compaction" in text and "literal indicators" in text


def test_audit_flags_reread_while_present_and_duplicates(cfg, tmp_path):
    steps = [tool_calls(read("ticket"), read("ticket")), tool_calls(read("ticket", 5, 3)), final("x")]
    run_dir, _ = mock_run(cfg, tmp_path, steps)
    c = analyze(run_dir)["tool_call_classification"]
    assert c == {"new": 1, "duplicate_in_turn": 1, "reread_while_present": 1}


def test_scoring_indicators_citations_flags_and_review_template(cfg, tmp_path):
    run_dir, _ = mock_run(cfg, tmp_path, compaction_steps("S"))
    s = score(run_dir)
    assert s["sections_present"] == ["Timeline", "Best-supported cause", "Competing explanation",
                                     "Recommended mitigation", "Uncertainty"]
    assert set(s["citations"]["invalid"]) == {"nonexistent:L1", "ticket:L9999"}
    assert all(s["criteria"]["diagnosis"]["indicators"].values())
    assert "config_change" in s["criteria"]["diagnosis"]["support_facts_cited"]
    assert any(f["flag"] == "fraud_screening" for f in s["constraint_flags"])  # a flag, for a human to read
    review = run_dir / "review.json"
    template = json.loads(review.read_text(encoding="utf-8"))
    assert template["criteria"]["timeline"]["verdict"] is None
    review.write_text(json.dumps({**template, "reviewer": "me"}), encoding="utf-8")
    score(run_dir)
    assert json.loads(review.read_text(encoding="utf-8"))["reviewer"] == "me"  # never overwritten


def test_not_testable_suggestion_when_no_compaction(cfg, tmp_path):
    run_dir, _ = mock_run(cfg, tmp_path, [tool_calls(read("ticket")), final(ANSWER)])
    assert score(run_dir)["suggested_failure_mode"]["mode"] == "not_testable"


def test_report_refuses_mock_runs(cfg, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    mock_run(cfg, runs, [final(ANSWER)])
    text = build(runs, out_dir=tmp_path)
    assert "No real model runs are recorded yet" in text and "provenance 'mock'" in text
    assert (tmp_path / "REPORT.md").exists()


def test_continuation_from_recorded_request(cfg, tmp_path):
    run_dir, _ = mock_run(cfg, tmp_path, compaction_steps("S-ONE"))
    state, parent = continuation_state(run_dir, 4)  # compaction happens before agent request 4
    assert state.summary == "S-ONE" and parent["at_agent_request"] == 4
    client = ScriptedClient([final(ANSWER)])
    cont_dir, result = start_run(cfg, client, "continuation", "mock", runs_dir=tmp_path, state=state, parent=parent)
    assert result["stop_reason"] == "final_answer"
    recorded = [json.loads(l) for l in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    original = [e for e in recorded if e["type"] == "model_request" and e["purpose"] == "agent"][3]["payload"]
    assert client.payloads[0]["messages"] == original["messages"]  # same decision point, same context
    assert replay(cont_dir)["ok"]
