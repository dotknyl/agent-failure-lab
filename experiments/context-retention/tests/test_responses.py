"""Responses API integration (stateless, store=false) with opaque encrypted reasoning carried across tool-call
continuations within one run. Offline: mock transport and scripted model only."""

import itertools
import json

import httpx2
import pytest

from conftest import ScriptedClient, summary, usage
from ctxlab.artifacts import TOOL_SCHEMAS
from ctxlab.client import OpenAIResponsesClient, normalize_responses, to_responses_wire
from ctxlab.config import preflight
from ctxlab.experiment import replay, start_run
from ctxlab.history import render_transcript
from ctxlab.probe import probe
from ctxlab.trace import load_events

KEY = "sk-test-responses-0123456789"
RS = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-opaque-blob"}


def raw_response(calls=(), text=None, reasoning=(RS,), status="completed", reason=None):
    output = list(reasoning)
    if text is not None:
        output.append({"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for i, (name, args) in enumerate(calls):
        output.append({"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{i}", "name": name,
                       "arguments": json.dumps(args), "status": "completed"})
    out = {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-6-luna", "status": status,
           "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
           "usage": {"input_tokens": 300, "input_tokens_details": {"cached_tokens": 100}, "output_tokens": 80,
                     "output_tokens_details": {"reasoning_tokens": 50}, "total_tokens": 380}}
    if reason:
        out["incomplete_details"] = {"reason": reason}
    return out


def logical_payload(messages, effort="medium"):
    return {"model": "gpt-6-luna", "messages": messages, "tools": TOOL_SCHEMAS, "max_completion_tokens": 900,
            "reasoning_effort": effort}


def test_wire_is_stateless_and_carries_reasoning_before_the_turns_calls():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": None, "reasoning_items": [RS], "reasoning_tokens_reported": 50,
             "tool_calls": [{"id": "call_0", "type": "function",
                             "function": {"name": "list_artifacts", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_0", "content": "result"}]
    w = to_responses_wire(logical_payload(msgs))
    assert w["store"] is False and "previous_response_id" not in w
    assert w["reasoning"] == {"effort": "medium"} and w["include"] == ["reasoning.encrypted_content"]
    assert w["max_output_tokens"] == 900 and w["tools"][0] == {
        "type": "function", "name": "list_artifacts", "description": TOOL_SCHEMAS[0]["function"]["description"],
        "parameters": TOOL_SCHEMAS[0]["function"]["parameters"], "strict": False}
    assert [i.get("type") or i["role"] for i in w["input"]] == ["system", "user", "reasoning", "function_call",
                                                               "function_call_output"]
    assert w["input"][2] == RS  # passed back byte-for-byte, never interpreted
    assert "reasoning_tokens_reported" not in json.dumps(w)


def test_normalize_maps_calls_reasoning_usage_and_truncation():
    n = normalize_responses(raw_response(calls=[("read_artifact", {"id": "ticket"})]))
    assert n["tool_calls"] == [{"id": "call_0", "name": "read_artifact", "arguments": '{"id": "ticket"}'}]
    assert n["reasoning_items"] == [RS] and n["finish_reason"] == "tool_calls"
    assert n["usage"] == {"prompt_tokens": 300, "completion_tokens": 80, "total_tokens": 380, "cached_tokens": 100,
                          "reasoning_tokens": 50}
    t = normalize_responses(raw_response(text="partial", status="incomplete", reason="max_output_tokens"))
    assert t["content"] == "partial" and t["finish_reason"] == "length"


def test_client_posts_stateless_request_and_maps_errors(monkeypatch):
    monkeypatch.setenv("CTXLAB_TEST_KEY", KEY)
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx2.Response(200, json=raw_response(calls=[("list_artifacts", {})]))
        return httpx2.Response(400, json={"error": {"message": f"bad {KEY}", "type": "x"}})

    client = OpenAIResponsesClient("https://example.invalid/v1", "CTXLAB_TEST_KEY", 10,
                                   http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    payload = logical_payload([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    out = client.complete(payload)
    assert seen[0].url.path.endswith("/responses") and json.loads(seen[0].content) == client.wire(payload)
    assert out["reasoning_items"] == [RS] and out["usage"]["reasoning_tokens"] == 50
    with pytest.raises(Exception) as e:
        client.complete(payload)
    assert e.value.status == 400 and KEY not in e.value.message and len(seen) == 2


_ids = itertools.count(100)


def resp(calls=(), text=None, reasoning_tokens=1_000):
    """Scripted normalized response as the Responses client returns it (unique call ids, like the API)."""
    n = normalize_responses(raw_response(calls=calls, text=text))
    for c in n["tool_calls"]:
        c["id"] = f"call_{next(_ids)}"
    n["usage"] = {**usage(), "reasoning_tokens": reasoning_tokens}
    return n


def test_runner_carries_reasoning_within_a_run_and_compaction_drops_it_with_summarized_groups(cfg, tmp_path):
    cfg.provider.integration = "openai-responses"
    cfg.provider.reasoning_effort = "medium"
    traces = lambda off: ("read_artifact", {"id": "traces-2026-10-06", "offset": off, "limit": 200})
    steps = [resp([traces(1)]), resp([traces(60)]), summary("S1"), resp(text="## Timeline\nx")]
    client = ScriptedClient(steps)
    run_dir, result = start_run(cfg, client, "natural", "mock", runs_dir=tmp_path)
    assert result["stop_reason"] == "final_answer" and result["totals"]["compactions"] == 1
    agent = [p for p in client.payloads if p.get("tools")]
    assert agent[0]["messages"][-1]["role"] == "user"  # a run starts with no carried reasoning
    carried = [m for m in agent[1]["messages"] if m.get("reasoning_items")]
    assert carried and carried[0]["reasoning_items"] == [RS] and carried[0]["reasoning_tokens_reported"] == 1_000
    summary_req = next(p for p in client.payloads if not p.get("tools"))
    assert "encrypted reasoning item(s): opaque" in summary_req["messages"][1]["content"]
    assert "gAAAA-opaque-blob" not in summary_req["messages"][1]["content"]
    after = agent[2]["messages"]
    assert sum(1 for m in after if m.get("reasoning_items")) == 1  # only the retained group keeps its reasoning
    assert replay(run_dir)["ok"]


def test_probe_requires_encrypted_reasoning_when_carrying(cfg, tmp_path):
    cfg.provider.integration = "openai-responses"
    cfg.provider.reasoning_effort = "medium"
    ok = ScriptedClient([resp([("list_artifacts", {})]), resp(text="12")])
    assert probe(cfg, ok, runs_dir=tmp_path / "a") == 0
    second = ok.payloads[1]["messages"]
    assert any(m.get("reasoning_items") == [RS] for m in second)  # re-sent on the continuation
    # Observed on the OpenAI API 2026-11-13: no reasoning on the trivial first step, reasoning on the second.
    no_rs = lambda r: {**r, "reasoning_items": []}
    late = ScriptedClient([no_rs(resp([("list_artifacts", {})])), resp(text="12"), resp(text="OK")])
    assert probe(cfg, late, runs_dir=tmp_path / "b") == 0
    third = late.payloads[2]["messages"]
    assert third[-2]["reasoning_items"] == [RS] and third[-1]["role"] == "user"  # carried back and accepted
    never = ScriptedClient([no_rs(resp([("list_artifacts", {})])), no_rs(resp(text="12"))])
    assert probe(cfg, never, runs_dir=tmp_path / "c") == 1


def test_preflight_rejects_streaming_with_responses(cfg):
    cfg.provider.integration = "openai-responses"
    cfg.provider.stream = True
    assert any("not implemented" in e for e in preflight(cfg, "natural")[0])
    cfg.provider.integration = "anthropic"
    assert any("integration" in e for e in preflight(cfg, "natural")[0])


def test_transcript_never_renders_encrypted_content():
    text = render_transcript([{"role": "assistant", "content": "c", "reasoning_items": [RS],
                               "tool_calls": [{"id": "x", "function": {"name": "n", "arguments": "{}"}}]}])
    assert "gAAAA" not in text and "1 encrypted reasoning item(s)" in text
