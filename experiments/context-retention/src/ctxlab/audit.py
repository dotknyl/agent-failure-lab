"""Evidence audit of a recorded run (offline, deterministic).

Answers, per run: what reached the model at each request, what each compaction kept or dropped, what was in the
request that produced the final answer, and why tools were called again.

Fact presence uses literal/regex probes from ``gold/.../facts.json``. A probe hit means the fact's *text* was in
the payload; a miss means it was not found *literally* (a paraphrase in a summary can still carry it). These are
indicators for a reviewer, not semantic judgments.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import prompts
from .paths import GOLD_DIR
from .trace import load_events, load_manifest


def load_facts(gold_dir: Path = GOLD_DIR) -> list[dict]:
    facts = json.loads((gold_dir / "facts.json").read_text(encoding="utf-8"))["facts"]
    for f in facts:
        f["_re"] = [re.compile(p, re.IGNORECASE) for p in f["probes"]]
    return facts


def message_text(m: dict) -> str:
    parts = [m.get("content") or ""]
    for c in m.get("tool_calls") or []:
        parts.append(f"{c['function']['name']}({c['function']['arguments']})")
    return "\n".join(parts)


def facts_in(text: str, facts: list[dict]) -> dict[str, bool]:
    return {f["id"]: any(r.search(text) for r in f["_re"]) for f in facts}


def regions(messages: list[dict]) -> dict[str, str]:
    """Split a payload into pinned (system+task), summary and body text."""
    pinned = "\n".join(message_text(m) for m in messages[:2])
    rest = messages[2:]
    summary = ""
    if rest and rest[0]["role"] == "user" and str(rest[0].get("content", "")).startswith(
            prompts.SUMMARY_MESSAGE_PREFIX):
        summary, rest = rest[0]["content"], rest[1:]
    return {"pinned": pinned, "summary": summary, "body": "\n".join(message_text(m) for m in rest)}


def fact_locations(messages: list[dict], facts: list[dict]) -> dict[str, list[str]]:
    regs = {k: facts_in(v, facts) for k, v in regions(messages).items()}
    return {f["id"]: [r for r in ("pinned", "summary", "body") if regs[r][f["id"]]] for f in facts}


def _norm_args(name: str, arguments: str) -> str:
    try:
        return name + json.dumps(json.loads(arguments or "{}"), sort_keys=True)
    except json.JSONDecodeError:
        return name + (arguments or "")


def analyze(run_dir: Path, gold_dir: Path = GOLD_DIR) -> dict:
    run_dir = Path(run_dir)
    events = load_events(run_dir)
    manifest = load_manifest(run_dir)
    facts = load_facts(gold_dir)

    agent_payloads: dict[int, dict] = {}  # agent request number -> payload (first attempt)
    requests = []
    pending = None
    for e in events:
        if e["type"] == "model_request":
            pending = {"purpose": e["purpose"], "attempt": e["attempt"], "agent_request": e["agent_request"],
                       "estimate_input_tokens": e["estimate_input_tokens"],
                       "messages": len(e["payload"]["messages"]),
                       # Responses API: encrypted reasoning carried in this request. Counted, never decoded.
                       "opaque_reasoning_items": sum(len(m.get("reasoning_items") or [])
                                                     for m in e["payload"]["messages"]),
                       "opaque_reasoning_chars": sum(len(json.dumps(m.get("reasoning_items")))
                                                     for m in e["payload"]["messages"] if m.get("reasoning_items"))}
            if e["purpose"] == "agent" and e["attempt"] == 1:
                agent_payloads[e["agent_request"]] = e["payload"]
                pending["fact_locations"] = fact_locations(e["payload"]["messages"], facts)
            requests.append(pending)
        elif e["type"] == "model_response" and pending is not None:
            u = e["response"].get("usage") or {}
            pending["prompt_tokens"] = u.get("prompt_tokens")
            pending["completion_tokens"] = u.get("completion_tokens")
            pending["finish_reason"] = e["response"].get("finish_reason")
            pending["tool_calls"] = len(e["response"].get("tool_calls") or [])
            if pending["prompt_tokens"]:
                pending["estimate_ratio"] = round(pending["estimate_input_tokens"] / pending["prompt_tokens"], 3)
        elif e["type"] == "model_error" and pending is not None:
            pending["error"] = e["error"]

    compactions = []
    for e in events:
        if e["type"] == "compaction_started":
            before = facts_in("\n".join(message_text(m) for m in e["context_before"]), facts)
            compactions.append({"n": e["n"], "estimate_before": e["estimate_before"], "threshold": e["threshold"],
                                "summarized_messages": e["summarized_messages"],
                                "retained_groups": e["retained_groups"], "_before": before})
        elif e["type"] == "compaction_finished":
            c = compactions[-1]
            in_summary = facts_in(e["summary"], facts)
            after = facts_in("\n".join(message_text(m) for m in e["context_after"]), facts)
            c.update({"estimate_after": e["estimate_after"], "summary_chars": len(e["summary"]),
                      "summary_truncated": e["summary_truncated"]})
            c["facts"] = {f["id"]: {"before": c["_before"][f["id"]], "in_summary": in_summary[f["id"]],
                                    "after": after[f["id"]]} for f in facts}
            c["literal_facts_lost"] = [k for k, v in c["facts"].items() if v["before"] and not v["after"]]
    for c in compactions:
        c.pop("_before", None)

    tool_calls = _classify_tool_calls(events, agent_payloads)
    counts: dict[str, int] = {}
    for t in tool_calls:
        counts[t["classification"]] = counts.get(t["classification"], 0) + 1

    finished = next((e for e in events if e["type"] == "run_finished"), None)
    final_text = (run_dir / "final.md").read_text(encoding="utf-8") if (run_dir / "final.md").exists() else None
    final_request = max(agent_payloads) if agent_payloads else None
    final_decision = None
    if final_request is not None:
        locs = fact_locations(agent_payloads[final_request]["messages"], facts)
        in_answer = facts_in(final_text or "", facts)
        final_decision = {"agent_request": final_request,
                          "facts": {f["id"]: {"in_request": locs[f["id"]], "in_answer": in_answer[f["id"]]}
                                    for f in facts}}
    return {
        "run_id": manifest["run_id"],
        "provenance": manifest["provenance"],
        "condition": manifest["condition"],
        "stop_reason": finished["stop_reason"] if finished else "interrupted",
        "totals": finished["totals"] if finished else None,
        "compaction_tested": bool(compactions),
        "requests": requests,
        "compactions": compactions,
        "tool_calls": tool_calls,
        "tool_call_classification": counts,
        "final_decision": final_decision,
        "fact_descriptions": {f["id"]: f["description"] for f in facts},
        "probe_semantics": "literal/regex indicator; miss != absent, hit != used",
    }


def _classify_tool_calls(events: list[dict], agent_payloads: dict[int, dict]) -> list[dict]:
    calls: dict[str, dict] = {}
    order: list[str] = []
    for e in events:
        if e["type"] == "tool_call":
            calls[e["call_id"]] = {"call_id": e["call_id"], "name": e["name"], "arguments": e["arguments"],
                                   "agent_request": e["agent_request"]}
            order.append(e["call_id"])
        elif e["type"] == "tool_result":
            calls[e["call_id"]]["ok"] = e["ok"]
            calls[e["call_id"]]["meta"] = e["meta"]
    out = []
    history: list[dict] = []
    for cid in order:
        c = calls[cid]
        payload = agent_payloads.get(c["agent_request"], {"messages": []})
        present_ids = {m.get("tool_call_id") for m in payload["messages"] if m.get("role") == "tool"}
        key = _norm_args(c["name"], c["arguments"])
        same_turn = [h for h in history if h["agent_request"] == c["agent_request"]]
        earlier = [h for h in history if h["agent_request"] < c["agent_request"]]
        if any(h["key"] == key for h in same_turn):
            cls = "duplicate_in_turn"
        else:
            overlap = [h for h in earlier if _overlaps(h, c, key)]
            if not overlap:
                cls = "new"
            elif any(h["call_id"] in present_ids for h in overlap):
                cls = "reread_while_present"
            else:
                cls = "reread_after_compaction"
        c2 = {k: v for k, v in c.items() if k != "meta"}
        c2["classification"] = cls
        if c.get("meta", {}).get("tool") == "read_artifact":
            m = c["meta"]
            c2["read"] = [m.get("artifact"), m.get("first_line"), m.get("last_line"), m.get("bounded_by")]
        out.append(c2)
        history.append({"call_id": cid, "agent_request": c["agent_request"], "key": key, "meta": c.get("meta", {}),
                        "name": c["name"]})
    return out


def _overlaps(h: dict, c: dict, key: str) -> bool:
    if h["key"] == key:
        return True
    hm, cm = h["meta"], c.get("meta", {})
    if hm.get("tool") == cm.get("tool") == "read_artifact" and hm.get("artifact") == cm.get("artifact"):
        if None in (hm.get("first_line"), hm.get("last_line"), cm.get("first_line"), cm.get("last_line")):
            return False
        return hm["first_line"] <= cm["last_line"] and cm["first_line"] <= hm["last_line"]
    return False


def render(a: dict) -> str:
    out = [f"run {a['run_id']}  provenance={a['provenance']}  condition={a['condition']}  stop={a['stop_reason']}"]
    t = a["totals"] or {}
    out.append(f"agent requests {t.get('agent_requests')}  summary requests {t.get('summary_requests')}  "
               f"attempts {t.get('attempts')} (failed {t.get('failed_attempts')})  tool calls {t.get('tool_calls')} "
               f"(errors {t.get('tool_errors')})  compactions {t.get('compactions')}")
    out.append(f"tokens: prompt {t.get('prompt_tokens')}  completion {t.get('completion_tokens')}  "
               f"(summaries: {t.get('summary_prompt_tokens')}/{t.get('summary_completion_tokens')})  "
               f"cost reported {t.get('cost_reported_usd')}  upper bound {t.get('cost_upper_bound_usd')}")
    out.append("")
    out.append("requests (purpose, attempt, estimate -> provider prompt_tokens, finish, tool calls):")
    for r in a["requests"]:
        out.append(f"  [{r['agent_request']:>2}] {r['purpose']:<7} a{r['attempt']} est {r['estimate_input_tokens']:>7} "
                   f"-> {r.get('prompt_tokens')!s:>7}  ratio {r.get('estimate_ratio')!s:<6} "
                   f"{r.get('finish_reason')!s:<10} calls {r.get('tool_calls', '-')}"
                   + (f"  opaque reasoning items {r['opaque_reasoning_items']}" if r["opaque_reasoning_items"] else "")
                   + (f"  ERROR {r['error']['kind']} {r['error'].get('status')}" if r.get("error") else ""))
    for c in a["compactions"]:
        out.append(f"compaction #{c['n']}: est {c['estimate_before']} -> {c.get('estimate_after')} (threshold "
                   f"{c['threshold']}), summarized {c['summarized_messages']} msgs, kept {c['retained_groups']} groups,"
                   f" summary {c.get('summary_chars')} chars{' TRUNCATED' if c.get('summary_truncated') else ''}")
        out.append(f"   literal facts lost: {', '.join(c.get('literal_facts_lost') or []) or 'none'}")
    out.append("")
    out.append(f"tool calls by classification: {a['tool_call_classification']}")
    for tc in a["tool_calls"]:
        if tc["classification"] != "new":
            out.append(f"  req {tc['agent_request']:>2} {tc['classification']:<24} {tc['name']} {tc['arguments']}")
    fd = a["final_decision"]
    if fd:
        out.append("")
        out.append(f"final decision (agent request {fd['agent_request']}): fact -> where present in request | "
                   "mentioned in answer")
        for fid, v in fd["facts"].items():
            out.append(f"  {fid:<34} {','.join(v['in_request']) or 'NOT FOUND (literal)':<22} | "
                       f"{'yes' if v['in_answer'] else 'no'}")
    out.append("")
    out.append("Fact probes are literal indicators: a miss is not proof of absence and a hit is not proof of use.")
    return "\n".join(out)
