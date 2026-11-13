"""Endpoint compatibility probe (PAID, two small requests).

Checks what the Lab depends on instead of assuming an "OpenAI-compatible" endpoint provides it: a tool call is
returned, a tool result is accepted in the next request, and usage is reported on both responses (also when
streaming). Recorded under ``runs/_probes/`` with provenance ``probe``; never part of results.
"""

from __future__ import annotations

import json
from pathlib import Path

from .artifacts import TOOL_SCHEMAS, ArtifactStore
from .client import ProviderError
from .config import RunConfig
from .paths import FIXTURE_DIR, RUNS_DIR
from .trace import Recorder
from .experiment import LedgerError, build_manifest, new_run_id, pilot_ledger

PROBE_MAX_OUTPUT = 1_000


def probe(cfg: RunConfig, client, runs_dir: Path = RUNS_DIR) -> int:
    store = ArtifactStore(FIXTURE_DIR)
    try:
        ledger = pilot_ledger(cfg, runs_dir)
    except LedgerError as e:
        print(f"ERROR {e}", flush=True)
        return 2
    worst = cfg.budget.worst_case(2_000, PROBE_MAX_OUTPUT)
    if worst is not None and 3 * worst > ledger["effective_cap_usd"]:
        print(f"ERROR probe worst case ${3 * worst:.4f} exceeds the remaining cap ${ledger['effective_cap_usd']:.4f}",
              flush=True)
        return 2
    run_id = new_run_id("probe")
    rec = Recorder(Path(runs_dir) / "_probes" / run_id, secrets=[getattr(client, "secret", "")])
    rec.write_json("manifest.json", build_manifest(run_id, cfg, store, "probe", "probe", ledger=ledger))
    totals = {"attempts": 0, "failed_attempts": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "cost_reported_usd": 0.0 if cfg.budget.priced else None,
              "cost_upper_bound_usd": 0.0 if cfg.budget.priced else None}

    def account(resp):
        u = resp.get("usage") or {}
        totals["attempts"] += 1
        totals["prompt_tokens"] += u.get("prompt_tokens") or 0
        totals["completion_tokens"] += u.get("completion_tokens") or 0
        if cfg.budget.priced:
            if u.get("prompt_tokens") is None:  # unknown usage: charge the worst case to the upper bound
                totals["cost_upper_bound_usd"] += worst
                return
            c = cfg.budget.cost(u["prompt_tokens"], u.get("completion_tokens") or 0, u.get("cached_tokens") or 0)
            totals["cost_reported_usd"] += c
            totals["cost_upper_bound_usd"] += c
    p = cfg.provider
    base = {"model": p.model, p.max_tokens_param: PROBE_MAX_OUTPUT, "tools": TOOL_SCHEMAS[:1]}
    if p.temperature is not None:
        base["temperature"] = p.temperature
    if p.reasoning_effort is not None:
        base["reasoning_effort"] = p.reasoning_effort
    carry_reasoning = p.integration == "openai-responses" and p.reasoning_effort not in (None, "none")
    wire = (lambda pl: {"wire_payload": client.wire(pl)}) if hasattr(client, "wire") else (lambda pl: {})
    if p.stream:
        base["stream"] = True
        base["stream_options"] = {"include_usage": True}
    messages = [{"role": "system", "content": "You are checking that tool calling works."},
                {"role": "user", "content": "Call list_artifacts once, then answer with only the number of "
                                            "artifacts it reports."}]
    checks = {}
    try:
        payload = {**base, "messages": messages}
        rec.event("model_request", purpose="probe", attempt=1, agent_request=1, estimate_input_tokens=0,
                  payload=payload, **wire(payload))
        r1 = client.complete(payload)
        account(r1)
        rec.event("model_response", purpose="probe", attempt=1, response=r1)
        checks["tool_call_returned"] = bool(r1["tool_calls"])
        checks["usage_on_tool_call_response"] = bool(r1.get("usage") and r1["usage"].get("prompt_tokens"))
        if not r1["tool_calls"]:
            raise ProviderError("probe", "no tool call returned")
        c = r1["tool_calls"][0]
        assistant = {"role": "assistant", "content": r1["content"], "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}]}
        if r1.get("reasoning_items"):
            assistant["reasoning_items"] = r1["reasoning_items"]  # carried back unchanged, as in the runner
        messages += [assistant,
                     {"role": "tool", "tool_call_id": c["id"], "content": store.call(c["name"], c["arguments"]).text}]
        payload = {**base, "messages": messages}
        rec.event("model_request", purpose="probe", attempt=1, agent_request=2, estimate_input_tokens=0,
                  payload=payload, **wire(payload))
        r2 = client.complete(payload)
        account(r2)
        rec.event("model_response", purpose="probe", attempt=1, response=r2)
        checks["tool_result_accepted"] = True
        checks["final_text_returned"] = bool(r2.get("content"))
        checks["usage_on_final_response"] = bool(r2.get("usage") and r2["usage"].get("prompt_tokens"))
        checks["answer"] = (r2.get("content") or "")[:200]
        if carry_reasoning:
            # A model may not reason on a trivial step (reasoning_tokens 0, no item), so accept encrypted
            # reasoning from either response, then require one request that carries it back to be accepted.
            encrypted = lambda r: any(i.get("encrypted_content") for i in r.get("reasoning_items") or [])
            checks["encrypted_reasoning_returned"] = encrypted(r1) or encrypted(r2)
            if encrypted(r1):
                checks["reasoning_carry_accepted"] = True  # request 2 already carried r1's reasoning
            elif encrypted(r2):
                messages += [{"role": "assistant", "content": r2.get("content") or "12",
                              "reasoning_items": r2["reasoning_items"]},
                             {"role": "user", "content": "Reply with only the word OK."}]
                payload = {**base, "messages": messages}
                rec.event("model_request", purpose="probe", attempt=1, agent_request=3, estimate_input_tokens=0,
                          payload=payload, **wire(payload))
                r3 = client.complete(payload)
                account(r3)
                rec.event("model_response", purpose="probe", attempt=1, response=r3)
                checks["reasoning_carry_accepted"] = True
    except ProviderError as e:
        rec.event("model_error", purpose="probe", attempt=1, error=e.to_dict())
        checks["error"] = e.to_dict()
        if e.kind != "probe":  # a failed HTTP attempt may still be billed: count its worst case
            totals["attempts"] += 1
            totals["failed_attempts"] += 1
            if cfg.budget.priced:
                totals["cost_upper_bound_usd"] += worst
    finally:
        rec.write_json("result.json", {**checks, "totals": totals})
        rec.close()
    print(json.dumps(checks, indent=2))
    required = ("tool_call_returned", "usage_on_tool_call_response", "tool_result_accepted",
                "usage_on_final_response")
    if carry_reasoning:  # the pilot depends on encrypted reasoning coming back and being accepted when re-sent
        required += ("encrypted_reasoning_returned", "reasoning_carry_accepted")
    ok = all(checks.get(k) for k in required)
    print("probe OK: endpoint supports what the Lab needs" if ok else "probe FAILED: do not run the pilot on this "
          "endpoint/config")
    return 0 if ok else 1
