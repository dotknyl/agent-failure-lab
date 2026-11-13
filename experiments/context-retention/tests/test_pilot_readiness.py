"""Pilot-level guarantees: one total budget across probe and runs, provenance hashes, frozen prompts, and the
manual-review requirement for evidence-based failure modes."""

import json

import pytest

from conftest import ScriptedClient, final, summary, tool_calls, usage
from ctxlab import prompts
from ctxlab.experiment import LedgerError, pilot_ledger, replay, start_run
from ctxlab.probe import probe
from ctxlab.report import failure_mode_cell

FROZEN_V1 = {
    "TASK": "55d04528f8300edb29620fd38f1eb22f14d76b4c90c7652fdd61bd2519e2d0e3",
    "AGENT_SYSTEM": "3c7b06dc488cb9d6ff67fefee45bec49cf036cd890ead55a989454db794ba882",
    "SUMMARY_SYSTEM": "ae81fdd07b30ecad4f2a113f0bae88dcad9cad5c63bb043c4cf82df69f5942e6",
    "SUMMARY_USER": "75f1b65b8db27e3e24ebefd457a4d6cb77ecb7da66d43c6fa1b1211a8f67ac7f",
    "FOCUSED_USER": "4720ee229b1e6ba04acafc8c1413b80ddc1719a6b5c0efd78a169128295f55f5",
}


def test_prompts_are_frozen_for_the_pilot():
    """Changing any prompt text requires a new PROMPT_VERSION and updated hashes here (and a README note)."""
    assert prompts.PROMPT_VERSION == "v1" and prompts.prompt_hashes() == FROZEN_V1


@pytest.fixture
def priced(cfg):
    cfg.budget.allow_without_dollar_cap = False
    cfg.budget.input_usd_per_mtok, cfg.budget.output_usd_per_mtok = 1.0, 10.0
    cfg.budget.pricing_source, cfg.budget.pricing_date = "test", "2026-11-13"
    cfg.budget.pilot_budget_usd, cfg.budget.max_cost_usd = 0.10, 0.10
    return cfg


def _real(cfg, runs, steps, condition="natural"):
    # Provenance "real" only so the ledger counts it; the client is still a scripted mock inside tmp_path.
    return start_run(cfg, ScriptedClient(steps), condition, "real", runs_dir=runs)


def test_ledger_spans_probe_and_runs_and_caps_the_next_run(priced, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    probe_steps = [tool_calls(("list_artifacts", {})), final("12")]
    assert probe(priced, ScriptedClient(probe_steps), runs_dir=runs) == 0
    probe_result = json.loads(next((runs / "_probes").glob("*/result.json")).read_text(encoding="utf-8"))
    assert probe_result["totals"]["cost_reported_usd"] == pytest.approx(2 * (100 * 1 + 20 * 10) / 1e6)

    _real(priced, runs, [final("a")], "focused")
    ledger = pilot_ledger(priced, runs)
    assert len(ledger["counted"]) == 2 and ledger["spent_upper_bound_usd"] > 0
    assert ledger["effective_cap_usd"] == pytest.approx(0.10 - ledger["spent_upper_bound_usd"])

    # Leave less than one worst-case request: the next run stops before sending anything.
    priced.budget.pilot_budget_usd = ledger["spent_upper_bound_usd"] + 0.001
    client = ScriptedClient([final("never")])
    run_dir, result = start_run(priced, client, "natural", "real", runs_dir=runs)
    assert result["stop_reason"] == "budget_cost" and client.payloads == []
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["budget_ledger"]["effective_cap_usd"] == pytest.approx(0.001, abs=1e-6)


def test_ledger_refuses_when_an_earlier_cost_is_unknown(priced, tmp_path):
    runs = tmp_path / "runs"
    run_dir, _ = _real(priced, runs, [final("a")], "focused")
    (run_dir / "result.json").unlink()  # an interrupted run: cost unknown
    with pytest.raises(LedgerError):
        start_run(priced, ScriptedClient([final("x")]), "natural", "real", runs_dir=runs)


def test_summary_calls_count_against_the_effective_cap_and_replay_uses_it(priced, tmp_path):
    runs = tmp_path / "runs"
    priced.budget.pilot_budget_usd = priced.budget.max_cost_usd = 5.0
    steps = [tool_calls(("read_artifact", {"id": "traces-2026-10-06", "limit": 200})),
             tool_calls(("read_artifact", {"id": "traces-2026-10-06", "offset": 60, "limit": 200})),
             summary("s"), final("done")]
    run_dir, result = _real(priced, runs, steps)
    t = result["totals"]
    assert t["summary_requests"] == 1
    assert t["cost_reported_usd"] == pytest.approx((t["prompt_tokens"] + 10 * t["completion_tokens"]) / 1e6)
    assert replay(run_dir)["ok"]


def test_manifest_records_code_lock_and_config_hashes(cfg, tmp_path):
    run_dir, _ = start_run(cfg, ScriptedClient([final("x")]), "natural", "mock", runs_dir=tmp_path)
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(m["code"]["src_sha256"]) == 64 and len(m["code"]["uv.lock_sha256"]) == 64
    assert len(m["config_sha256"]) == 64 and "git_head" in m["code"]


def test_evidence_failure_modes_need_inspected_references():
    assert failure_mode_cell(None) == "unreviewed"
    assert "UNSUPPORTED" in failure_mode_cell({"failure_mode": "evidence_missing_at_decision"})
    assert "UNSUPPORTED" in failure_mode_cell({"failure_mode": "evidence_present_misused",
                                               "evidence_inspection": [{"fact": "config_change"}]})
    ok = {"failure_mode": "evidence_present_misused", "evidence_inspection": [
        {"fact": "config_change", "where": "agent request 9", "event_seq": 41,
         "quote": "+  request_timeout_ms: 8000"}]}
    assert failure_mode_cell(ok) == "evidence_present_misused"
    assert failure_mode_cell({"failure_mode": "resource_limit"}) == "resource_limit"


def test_preflight_requires_a_pilot_budget_when_priced(priced):
    from ctxlab.config import preflight
    priced.budget.pilot_budget_usd = 0
    assert any("pilot_budget_usd" in e for e in preflight(priced, "natural")[0])
    priced.budget.pilot_budget_usd, priced.budget.max_cost_usd = 1.0, 2.0
    assert any("cannot exceed" in e for e in preflight(priced, "natural")[0])
