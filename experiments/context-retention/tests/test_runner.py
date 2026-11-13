"""Agent loop, automatic compaction, termination and accounting, driven by a scripted MOCK model."""

import json

import pytest

from conftest import ScriptedClient, final, summary, tool_calls, usage
from ctxlab import prompts
from ctxlab.client import ProviderError
from ctxlab.experiment import start_run
from ctxlab.history import validate
from ctxlab.trace import load_events

TRACES = "traces-2026-10-06"
GOLD_WORDS = ["amplif", "root cause", "rubric", "gold", "cfgctl rollback checkout-api/payment_client --to 17"]


def read(aid, offset=1, limit=200):
    return ("read_artifact", {"id": aid, "offset": offset, "limit": limit})


def run(cfg, steps, tmp_path, condition="natural"):
    client = ScriptedClient(steps)
    run_dir, result = start_run(cfg, client, condition, "mock", runs_dir=tmp_path)
    return run_dir, result, client


def agent_payloads(client):
    return [p for p in client.payloads if p.get("tools")]


def test_natural_run_compacts_at_group_boundary_and_continues(cfg, tmp_path):
    steps = [tool_calls(read(TRACES)), tool_calls(read(TRACES, 60)), summary("S1: read traces lines 1-120."),
             tool_calls(read("config-diff")), final("## Timeline\nx")]
    run_dir, result, client = run(cfg, steps, tmp_path)
    assert result["stop_reason"] == "final_answer"
    assert result["totals"]["compactions"] == 1 and result["totals"]["summary_requests"] == 1
    for p in agent_payloads(client):
        validate(p["messages"])
        assert p["messages"][0]["content"] == prompts.AGENT_SYSTEM and p["messages"][1]["content"] == prompts.TASK
    after = agent_payloads(client)[2]["messages"]
    assert after[2]["content"].startswith(prompts.SUMMARY_MESSAGE_PREFIX) and "S1:" in after[2]["content"]
    events = load_events(run_dir)
    calls_before = [e["call_id"] for e in events[:next(i for i, e in enumerate(events)
                                                          if e["type"] == "compaction_started")]
                    if e["type"] == "tool_call"]
    assert [m["tool_call_id"] for m in after if m["role"] == "tool"] == calls_before[-1:]  # latest group kept
    started = next(e for e in events if e["type"] == "compaction_started")
    finished = next(e for e in events if e["type"] == "compaction_finished")
    assert started["estimate_before"] + cfg.context.max_output_tokens >= cfg.context.compaction_threshold_tokens
    assert finished["estimate_after"] < started["estimate_before"]
    assert (run_dir / "final.md").read_text(encoding="utf-8").startswith("## Timeline")


def test_summarizer_gets_no_gold_and_folds_previous_summary(cfg, tmp_path):
    # base ~0.9K + each 200-line trace read ~5.4K (char budget): compaction before requests 3 and 4.
    steps = [tool_calls(read(TRACES)), tool_calls(read(TRACES, 60)), summary("FIRST-SUMMARY"),
             tool_calls(read(TRACES, 120)), summary("SECOND"), final("done")]
    _, result, client = run(cfg, steps, tmp_path)
    assert result["totals"]["compactions"] == 2
    summaries = [p for p in client.payloads if not p.get("tools")]
    assert "(none)" in summaries[0]["messages"][1]["content"]
    assert "FIRST-SUMMARY" in summaries[1]["messages"][1]["content"]
    for p in summaries:
        text = json.dumps(p).lower()
        assert not any(w.lower() in text for w in GOLD_WORDS)
        assert p["messages"][0]["content"] == prompts.SUMMARY_SYSTEM


def test_ineffective_compaction_stops_explicitly(cfg, tmp_path):
    cfg.context.compaction_threshold_tokens = 7_000
    steps = [tool_calls(read("ticket")), tool_calls(read(TRACES)), summary("s")]
    _, result, _ = run(cfg, steps, tmp_path)
    assert result["stop_reason"] == "compaction_ineffective"


def test_nothing_to_summarize_stops_explicitly(cfg, tmp_path):
    cfg.context.compaction_threshold_tokens = 5_000
    _, result, client = run(cfg, [tool_calls(read(TRACES))], tmp_path)
    assert result["stop_reason"] == "compaction_nothing_to_summarize" and len(client.payloads) == 1


def test_oversized_summary_is_an_ineffective_compaction(cfg, tmp_path):
    # A summary that is itself huge leaves the context over the threshold: stop instead of looping.
    steps = [tool_calls(read(TRACES)), tool_calls(read(TRACES, 60)), summary("s" * 36_000)]
    _, result, _ = run(cfg, steps, tmp_path)
    assert result["stop_reason"] == "compaction_ineffective"


def test_compaction_without_progress_guard(cfg, tmp_path):
    """Defensive guard: a second compaction with no new tool results since the first one is refused."""
    from ctxlab.artifacts import ArtifactStore
    from ctxlab.paths import FIXTURE_DIR
    from ctxlab.runner import Runner, Stop, initial_state
    from ctxlab.trace import MemoryRecorder
    runner = Runner(ArtifactStore(FIXTURE_DIR), ScriptedClient([]), MemoryRecorder(), cfg, "natural")
    runner.totals.compactions = 1
    state = initial_state("natural", runner.store)
    state.body = [{"role": "assistant", "content": "x" * 40_000}]
    with pytest.raises(Stop) as e:
        runner._maybe_compact(state)
    assert e.value.reason == "compaction_without_progress"


def test_summary_request_must_fit_native_window(cfg, tmp_path):
    cfg.context.native_context_tokens = 13_000
    cfg.context.summary_max_output_tokens = 8_000
    steps = [tool_calls(read(TRACES)), tool_calls(read(TRACES, 60))]
    _, result, _ = run(cfg, steps, tmp_path)
    assert result["stop_reason"] == "compaction_input_too_large"


def test_request_too_large_without_compaction(cfg, tmp_path):
    cfg.context.compaction_enabled = False
    cfg.context.native_context_tokens = 9_000
    _, result, _ = run(cfg, [tool_calls(read(TRACES)), tool_calls(read(TRACES, 60))], tmp_path)
    assert result["stop_reason"] == "request_too_large"


def test_max_tool_calls_answers_all_issued_calls_first(cfg, tmp_path):
    cfg.limits.max_tool_calls = 2
    run_dir, result, _ = run(cfg, [tool_calls(read("ticket"), read("config-diff"), read("runbook-payments"))],
                             tmp_path)
    assert result["stop_reason"] == "max_tool_calls"
    final_messages = next(e for e in load_events(run_dir) if e["type"] == "run_finished")["final_messages"]
    validate(final_messages)


def test_max_agent_requests(cfg, tmp_path):
    cfg.limits.max_agent_requests = 2
    _, result, _ = run(cfg, [tool_calls(read("ticket")), tool_calls(read("ticket", 5))], tmp_path)
    assert result["stop_reason"] == "max_agent_requests"


def test_retry_then_success_is_recorded_and_counted(cfg, tmp_path):
    steps = [ProviderError("http_status", "busy", 503, True), final("ok")]
    run_dir, result, client = run(cfg, steps, tmp_path)
    assert result["stop_reason"] == "final_answer"
    assert result["totals"]["attempts"] == 2 and result["totals"]["failed_attempts"] == 1
    assert result["totals"]["unreported_tokens_upper_bound"] > 0
    assert client.payloads[0] == client.payloads[1]
    assert [e["type"] for e in load_events(run_dir)].count("model_error") == 1


def test_non_retryable_and_exhausted_retries_stop(cfg, tmp_path):
    _, r1, _ = run(cfg, [ProviderError("http_status", "bad", 400, False)], tmp_path / "a")
    assert r1["stop_reason"] == "provider_error"
    busy = ProviderError("http_status", "busy", 503, True)
    _, r2, c2 = run(cfg, [busy, busy], tmp_path / "b")
    assert r2["stop_reason"] == "provider_error" and len(c2.payloads) == cfg.provider.max_retries + 1


def test_missing_usage_stops_when_required(cfg, tmp_path):
    resp = final("x")
    resp["usage"] = None
    _, result, _ = run(cfg, [resp], tmp_path)
    assert result["stop_reason"] == "usage_unavailable"


def test_cost_budget_stops_before_sending(cfg, tmp_path):
    cfg.budget.allow_without_dollar_cap = False
    cfg.budget.input_usd_per_mtok = 1.0
    cfg.budget.output_usd_per_mtok = 4.0
    cfg.budget.max_cost_usd = 0.001
    _, result, client = run(cfg, [final("never")], tmp_path)
    assert result["stop_reason"] == "budget_cost" and client.payloads == []


def test_cost_accounting_includes_summaries(cfg, tmp_path):
    cfg.budget.input_usd_per_mtok = 1.0
    cfg.budget.output_usd_per_mtok = 10.0
    cfg.budget.max_cost_usd = 5.0
    steps = [tool_calls(read(TRACES)), tool_calls(read(TRACES, 60)), summary("s"), final("done")]
    _, result, _ = run(cfg, steps, tmp_path)
    t = result["totals"]
    expected = (t["prompt_tokens"] * 1.0 + t["completion_tokens"] * 10.0) / 1e6
    assert t["summary_prompt_tokens"] == 500 and t["cost_reported_usd"] == pytest.approx(expected)


def test_token_budget_stops(cfg, tmp_path):
    cfg.limits.max_total_tokens = 2_000  # first request worst case: ceil(~0.9K * 1.5) + 1K output
    _, result, client = run(cfg, [final("x")], tmp_path)
    assert result["stop_reason"] == "budget_tokens" and client.payloads == []


def test_empty_and_truncated_final(cfg, tmp_path):
    empty = final("")
    _, r1, _ = run(cfg, [empty], tmp_path / "a")
    assert r1["stop_reason"] == "empty_response"
    _, r2, _ = run(cfg, [final("partial", finish="length")], tmp_path / "b")
    assert r2["stop_reason"] == "final_answer_truncated"


def test_duplicate_tool_call_ids_from_model_are_refused(cfg, tmp_path):
    bad = {"id": "r", "content": None, "finish_reason": "tool_calls", "usage": usage(),
           "tool_calls": [{"id": "x", "name": "list_artifacts", "arguments": "{}"},
                          {"id": "x", "name": "list_artifacts", "arguments": "{}"}]}
    _, result, _ = run(cfg, [bad], tmp_path)
    assert result["stop_reason"] == "invalid_history"


def test_focused_condition_sends_excerpts_without_tools(cfg, tmp_path):
    _, result, client = run(cfg, [final("## Timeline\n...")], tmp_path, condition="focused")
    assert result["stop_reason"] == "final_answer"
    p = client.payloads[0]
    assert "tools" not in p and "config-diff" in p["messages"][1]["content"]
    assert "8000" in p["messages"][1]["content"]


def test_streaming_and_param_options_shape_the_payload(cfg, tmp_path):
    cfg.provider.stream = True
    cfg.provider.max_tokens_param = "max_tokens"
    cfg.provider.temperature = 0.0
    _, _, client = run(cfg, [final("x")], tmp_path)
    p = client.payloads[0]
    assert p["stream"] is True and p["stream_options"] == {"include_usage": True}
    assert p["max_tokens"] == cfg.context.max_output_tokens and "max_completion_tokens" not in p
    assert p["temperature"] == 0.0


def test_reasoning_effort_is_sent_only_when_configured(cfg, tmp_path):
    from ctxlab.config import preflight
    _, _, c1 = run(cfg, [final("x")], tmp_path / "a")
    assert "reasoning_effort" not in c1.payloads[0]
    cfg.context.compaction_threshold_tokens = 12_000
    cfg.provider.reasoning_effort = "none"
    steps = [tool_calls(read(TRACES)), tool_calls(read(TRACES, 60)), summary("s"), final("done")]
    _, _, c2 = run(cfg, steps, tmp_path / "b")
    assert all(p["reasoning_effort"] == "none" for p in c2.payloads)  # agent and summary requests alike
    cfg.provider.reasoning_effort = "turbo"
    assert any("reasoning_effort" in e for e in preflight(cfg, "natural")[0])


def test_trace_redacts_secrets(cfg, tmp_path):
    client = ScriptedClient([final("the key is sk-test-SECRET123")])
    run_dir, result = start_run(cfg, client, "natural", "mock", runs_dir=tmp_path, secrets_=["sk-test-SECRET123"])
    assert "sk-test-SECRET123" not in (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert result["redactions"] >= 1
