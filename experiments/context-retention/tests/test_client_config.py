"""The real OpenAI client wrapper against a mock HTTP transport (no network), and live-run preflight rules."""

import json

import httpx2
import pytest

from ctxlab.artifacts import TOOL_SCHEMAS
from ctxlab.client import OpenAIChatClient, ProviderError
from ctxlab.config import RunConfig, preflight
from ctxlab.paths import EXPERIMENT_DIR

KEY = "sk-test-0123456789abcdef"


def completion(tool=True):
    msg = {"role": "assistant", "content": None if tool else "done"}
    if tool:
        msg["tool_calls"] = [{"id": "call_1", "type": "function",
                              "function": {"name": "read_artifact", "arguments": "{\"id\":\"ticket\"}"}}]
    return {"id": "cmpl-1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127,
                      "prompt_tokens_details": {"cached_tokens": 64}}}


def sse(chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def make_client(monkeypatch, handler):
    monkeypatch.setenv("CTXLAB_TEST_KEY", KEY)
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    http = httpx2.Client(transport=httpx2.MockTransport(wrapped))
    return OpenAIChatClient("https://example.invalid/v1", "CTXLAB_TEST_KEY", 10, http_client=http), seen


PAYLOAD = {"model": "m", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
           "max_completion_tokens": 50}


def test_non_streamed_completion_is_normalized(monkeypatch):
    client, seen = make_client(monkeypatch, lambda r: httpx2.Response(200, json=completion()))
    out = client.complete(PAYLOAD)
    assert out["tool_calls"] == [{"id": "call_1", "name": "read_artifact", "arguments": "{\"id\":\"ticket\"}"}]
    assert out["usage"]["prompt_tokens"] == 120 and out["usage"]["cached_tokens"] == 64
    sent = json.loads(seen[0].content)
    assert sent == PAYLOAD  # the recorded payload is exactly what goes on the wire
    assert seen[0].url.path.endswith("/chat/completions")


def test_streamed_completion_accumulates_tool_calls_and_usage(monkeypatch):
    base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_9", "type": "function", "function": {"name": "list_artifacts", "arguments": ""}}]},
            "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
                              "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {**base, "choices": [], "usage": {"prompt_tokens": 33, "completion_tokens": 4, "total_tokens": 37}},
    ]
    client, seen = make_client(monkeypatch, lambda r: sse(chunks))
    payload = {**PAYLOAD, "tools": TOOL_SCHEMAS, "stream": True, "stream_options": {"include_usage": True}}
    out = client.complete(payload)
    assert out["tool_calls"] == [{"id": "call_9", "name": "list_artifacts", "arguments": "{}"}]
    assert out["usage"]["prompt_tokens"] == 33 and out["finish_reason"] == "tool_calls"
    sent = json.loads(seen[0].content)
    assert sent == payload  # agent-shaped payload (with tools) goes on the wire unchanged


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_streamed_truncated_reply_keeps_text_and_finish_reason(monkeypatch, finish):
    """The SDK's parsing stream helper raises on these; the Lab must keep the text and report the reason."""
    base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "## Timeline\n- 14:"},
                              "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"content": "04 release"}, "finish_reason": finish}]},
        {**base, "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59}},
    ]
    client, _ = make_client(monkeypatch, lambda r: sse(chunks))
    out = client.complete({**PAYLOAD, "tools": TOOL_SCHEMAS, "stream": True,
                           "stream_options": {"include_usage": True}})
    assert out["content"] == "## Timeline\n- 14:04 release" and out["finish_reason"] == finish
    assert out["usage"]["completion_tokens"] == 9 and out["tool_calls"] == []


def test_non_streamed_length_finish_is_returned_not_raised(monkeypatch):
    body = completion(tool=False)
    body["choices"][0]["finish_reason"] = "length"
    client, _ = make_client(monkeypatch, lambda r: httpx2.Response(200, json=body))
    out = client.complete({**PAYLOAD, "tools": TOOL_SCHEMAS})
    assert out["finish_reason"] == "length" and out["content"] == "done"


def test_stream_without_usage_is_reported_as_missing(monkeypatch):
    base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"},
                                    "finish_reason": "stop"}]}]
    client, _ = make_client(monkeypatch, lambda r: sse(chunks))
    assert client.complete({**PAYLOAD, "stream": True, "stream_options": {"include_usage": True}})["usage"] is None


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False), (401, False)])
def test_http_errors_map_to_provider_errors_without_leaking_the_key(monkeypatch, status, retryable):
    client, seen = make_client(monkeypatch, lambda r: httpx2.Response(
        status, json={"error": {"message": f"bad key {KEY}", "type": "x"}}))
    with pytest.raises(ProviderError) as e:
        client.complete(PAYLOAD)
    assert e.value.status == status and e.value.retryable is retryable and KEY not in e.value.message
    assert len(seen) == 1  # SDK retries are disabled; the runner owns retries


def test_missing_key_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("CTXLAB_ABSENT_KEY", raising=False)
    with pytest.raises(ProviderError):
        OpenAIChatClient("https://example.invalid/v1", "CTXLAB_ABSENT_KEY", 10)


def test_example_config_is_blocked_until_filled():
    cfg = RunConfig.load(EXPERIMENT_DIR / "configs" / "live.example.toml")
    errors, _ = preflight(cfg, "natural")
    joined = " ".join(errors)
    for needed in ("provider.model", "native_context_tokens", "authorized_by"):
        assert needed in joined


def test_preflight_requires_dollar_cap_or_explicit_waiver(cfg):
    cfg.budget.allow_without_dollar_cap = False
    errors, _ = preflight(cfg, "natural")
    assert any("no pricing configured" in e for e in errors)
    cfg.budget.input_usd_per_mtok, cfg.budget.output_usd_per_mtok = 1.0, 2.0
    errors, _ = preflight(cfg, "natural")
    assert any("max_cost_usd" in e for e in errors) and any("pricing_source" in e for e in errors)
    cfg.budget.max_cost_usd, cfg.budget.pricing_source, cfg.budget.pricing_date = 1.0, "provider page", "2026-11-13"
    cfg.budget.pilot_budget_usd = 1.0
    errors, notes = preflight(cfg, "natural")
    assert errors == [] and any("stress setting" in n for n in notes)


def test_preflight_rejects_threshold_above_native_and_oversized_retention(cfg):
    cfg.context.compaction_threshold_tokens = cfg.context.native_context_tokens + 1
    assert any("cannot exceed" in e for e in preflight(cfg, "natural")[0])
    cfg.context.compaction_threshold_tokens = 10_000
    cfg.context.retain_budget_tokens = 6_000
    assert any("retain_budget_tokens" in e for e in preflight(cfg, "natural")[0])


def test_cli_live_refuses_without_confirm_spend(tmp_path, monkeypatch, capsys):
    from ctxlab.cli import main
    cfg_text = (EXPERIMENT_DIR / "configs" / "live.example.toml").read_text(encoding="utf-8")
    assert main(["live", "run", "--condition", "natural", "--config",
                 str(EXPERIMENT_DIR / "configs" / "live.example.toml")]) == 2
    assert "preflight failed" in capsys.readouterr().err
    assert "model" in cfg_text
