"""The agent loop with automatic, model-generated compaction.

1. Send the task; the model chooses tools.
2. Append the assistant message and one tool result per tool call (always complete groups).
3. Before every agent request, estimate the payload (instructions + tool schemas + history) plus the output
   reserve. If it reaches the configured threshold, summarize older complete groups with a real model call,
   keep the most recent groups, and continue.
4. Stop on a final answer or on an explicit limit/error. Every stop has a named reason; nothing is dropped
   silently.
"""

from __future__ import annotations

import copy
import math
import time
from dataclasses import asdict, dataclass

from . import prompts
from .artifacts import TOOL_SCHEMAS, ArtifactStore
from .client import ChatClient, ProviderError
from .config import RunConfig
from .history import ConversationState, InvalidHistory, plan_compaction, render_transcript, validate
from .tokens import estimate_payload

STOP_REASONS = {
    "final_answer": "The model replied without tool calls.",
    "final_answer_truncated": "The final reply hit the output limit (finish_reason=length).",
    "empty_response": "The model returned neither content nor tool calls.",
    "max_agent_requests": "limits.max_agent_requests reached.",
    "max_tool_calls": "limits.max_tool_calls reached (all issued calls were answered first).",
    "max_compactions": "limits.max_compactions reached and the context is over the threshold again.",
    "compaction_without_progress": "Compaction was needed again with no new tool results since the last one.",
    "compaction_nothing_to_summarize": "Over the threshold, but there are no older complete groups to summarize.",
    "compaction_ineffective": "After compaction the estimate is still over the threshold.",
    "compaction_input_too_large": "The summarization request itself would not fit the native context window.",
    "compaction_failed": "The summarization call returned no text.",
    "request_too_large": "The next request would not fit the native context window.",
    "invalid_history": "The message sequence failed validation before sending.",
    "budget_tokens": "The next request could exceed limits.max_total_tokens.",
    "budget_cost": "The next request could exceed the dollar cap (budget.max_cost_usd, or what remains of "
                   "budget.pilot_budget_usd after earlier recorded runs and probes).",
    "provider_error": "A provider error persisted after the allowed retries, or was not retryable.",
    "usage_unavailable": "The provider returned no usage, so spending could not be accounted.",
}


class Stop(Exception):
    def __init__(self, reason: str, detail: str = ""):
        assert reason in STOP_REASONS, reason
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Totals:
    agent_requests: int = 0
    summary_requests: int = 0
    attempts: int = 0
    failed_attempts: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    compactions: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    summary_prompt_tokens: int = 0
    summary_completion_tokens: int = 0
    unreported_tokens_upper_bound: int = 0  # failed attempts: unknown whether billed
    cost_reported_usd: float | None = 0.0
    cost_upper_bound_usd: float | None = 0.0


class Runner:
    def __init__(self, store: ArtifactStore, client: ChatClient, recorder, cfg: RunConfig, condition: str,
                 tools_enabled: bool = True, cost_cap_usd: float | None = None):
        self.store = store
        self.client = client
        self.rec = recorder
        self.cfg = cfg
        self.condition = condition
        self.tools_enabled = tools_enabled
        self.compaction_active = condition in ("natural", "continuation") and cfg.context.compaction_enabled
        self.totals = Totals()
        # Effective dollar cap for this run: the per-run cap, or less when the pilot ledger says less remains.
        self.cost_cap_usd = cfg.budget.max_cost_usd if cost_cap_usd is None else cost_cap_usd
        if not cfg.budget.priced:
            self.totals.cost_reported_usd = None
            self.totals.cost_upper_bound_usd = None
        self.tool_results_since_compaction = 0
        self.final_answer: str | None = None

    # ------------------------------------------------------------------ payloads

    def build_payload(self, messages: list[dict], purpose: str) -> dict:
        p, c = self.cfg.provider, self.cfg.context
        payload: dict = {"model": p.model, "messages": copy.deepcopy(messages)}
        if purpose == "agent" and self.tools_enabled:
            payload["tools"] = copy.deepcopy(TOOL_SCHEMAS)
        payload[p.max_tokens_param] = c.max_output_tokens if purpose == "agent" else c.summary_max_output_tokens
        if p.temperature is not None:
            payload["temperature"] = p.temperature
        if p.reasoning_effort is not None:
            payload["reasoning_effort"] = p.reasoning_effort
        if p.stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    def estimate(self, payload: dict) -> int:
        return estimate_payload(payload, self.cfg.context.chars_per_token)

    # ------------------------------------------------------------------ main loop

    def run(self, state: ConversationState, extra_start: dict | None = None) -> dict:
        self.rec.event("run_started", condition=self.condition, tools_enabled=self.tools_enabled,
                       compaction_active=self.compaction_active, initial_messages=state.messages(),
                       **(extra_start or {}))
        stop: Stop
        try:
            while True:
                self._agent_turn(state)
        except Stop as s:
            stop = s
        result = {
            "stop_reason": stop.reason,
            "stop_detail": stop.detail,
            "stop_meaning": STOP_REASONS[stop.reason],
            "final_answer_present": self.final_answer is not None,
            "totals": asdict(self.totals),
        }
        self.rec.event("run_finished", **result, final_messages=state.messages())
        return result

    def _agent_turn(self, state: ConversationState) -> None:
        if self.totals.agent_requests >= self.cfg.limits.max_agent_requests:
            raise Stop("max_agent_requests")
        if self.compaction_active:
            self._maybe_compact(state)
        payload = self.build_payload(state.messages(), "agent")
        est = self.estimate(payload)
        if est + self.cfg.context.max_output_tokens > self.cfg.context.native_context_tokens:
            raise Stop("request_too_large", f"estimate {est} + output {self.cfg.context.max_output_tokens} > "
                                            f"native {self.cfg.context.native_context_tokens}")
        try:
            validate(payload["messages"])
        except InvalidHistory as e:
            raise Stop("invalid_history", str(e)) from None
        self.totals.agent_requests += 1
        resp = self._call(payload, "agent", est)
        self._handle(state, resp)

    def _handle(self, state: ConversationState, resp: dict) -> None:
        calls = resp.get("tool_calls") or []
        content = resp.get("content")
        if not calls:
            if not content:
                raise Stop("empty_response", f"finish_reason={resp.get('finish_reason')}")
            state.body.append({"role": "assistant", "content": content})
            self.final_answer = content
            self.rec.write_text("final.md", content if content.endswith("\n") else content + "\n")
            raise Stop("final_answer_truncated" if resp.get("finish_reason") == "length" else "final_answer")
        msg = {"role": "assistant", "content": content,
               "tool_calls": [{"id": c["id"], "type": "function",
                               "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}
        if resp.get("reasoning_items"):
            # Responses API only: opaque encrypted reasoning, carried with this turn's tool calls so the next
            # request continues it (dropped only when compaction summarizes this group). Never interpreted.
            msg["reasoning_items"] = resp["reasoning_items"]
            msg["reasoning_tokens_reported"] = (resp.get("usage") or {}).get("reasoning_tokens") or 0
        try:
            validate([{"role": "system", "content": ""}, *state.body, msg,
                      *[{"role": "tool", "tool_call_id": c["id"], "content": ""} for c in calls]])
        except InvalidHistory as e:
            raise Stop("invalid_history", f"model tool calls: {e}") from None
        state.body.append(msg)
        for c in calls:
            self.totals.tool_calls += 1
            self.rec.event("tool_call", call_id=c["id"], name=c["name"], arguments=c["arguments"],
                           agent_request=self.totals.agent_requests)
            outcome = self.store.call(c["name"], c["arguments"])
            if not outcome.ok:
                self.totals.tool_errors += 1
            state.body.append({"role": "tool", "tool_call_id": c["id"], "content": outcome.text})
            self.tool_results_since_compaction += 1
            self.rec.event("tool_result", call_id=c["id"], ok=outcome.ok, meta=outcome.meta, content=outcome.text)
        if self.totals.tool_calls >= self.cfg.limits.max_tool_calls:
            raise Stop("max_tool_calls")

    # ------------------------------------------------------------------ compaction

    def _maybe_compact(self, state: ConversationState) -> None:
        c = self.cfg.context
        est = self.estimate(self.build_payload(state.messages(), "agent"))
        if est + c.max_output_tokens < c.compaction_threshold_tokens:
            return
        if self.totals.compactions >= self.cfg.limits.max_compactions:
            raise Stop("max_compactions", f"estimate {est}")
        if self.totals.compactions > 0 and self.tool_results_since_compaction == 0:
            raise Stop("compaction_without_progress", f"estimate {est}")
        plan = plan_compaction(state, c.keep_recent_groups, c.retain_budget_tokens, c.chars_per_token)
        if plan is None:
            raise Stop("compaction_nothing_to_summarize", f"estimate {est}")
        n = self.totals.compactions + 1
        summary_payload = self.build_payload([
            {"role": "system", "content": prompts.SUMMARY_SYSTEM},
            {"role": "user", "content": prompts.SUMMARY_USER.format(
                task=prompts.TASK, previous=state.summary or "(none)",
                transcript=render_transcript(plan.to_summarize), max_words=c.summary_max_words)},
        ], "summary")
        s_est = self.estimate(summary_payload)
        if s_est + c.summary_max_output_tokens > c.native_context_tokens:
            raise Stop("compaction_input_too_large", f"summary request estimate {s_est}")
        tool_ids = lambda msgs: [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
        self.rec.event("compaction_started", n=n, estimate_before=est, threshold=c.compaction_threshold_tokens,
                       output_reserve=c.max_output_tokens, summarized_messages=len(plan.to_summarize),
                       summarized_groups=plan.summarized_groups, retained_groups=plan.retained_groups,
                       summarized_tool_call_ids=tool_ids(plan.to_summarize),
                       retained_tool_call_ids=tool_ids(plan.retained), previous_summary_present=bool(state.summary),
                       context_before=state.messages())
        resp = self._call(summary_payload, "summary", s_est)
        text = (resp.get("content") or "").strip()
        if not text:
            raise Stop("compaction_failed", f"finish_reason={resp.get('finish_reason')}")
        state.summary = text
        state.summary_message = {"role": "user", "content": prompts.summary_message(
            n, len(plan.to_summarize) + (1 if state.summary_message else 0), text)}
        state.body = plan.retained
        self.totals.compactions = n
        self.tool_results_since_compaction = 0
        after = self.estimate(self.build_payload(state.messages(), "agent"))
        self.rec.event("compaction_finished", n=n, summary=text, summary_truncated=resp.get("finish_reason") ==
                       "length", estimate_after=after, context_after=state.messages())
        if after + c.max_output_tokens >= c.compaction_threshold_tokens:
            raise Stop("compaction_ineffective", f"estimate after compaction {after}")

    # ------------------------------------------------------------------ provider calls and accounting

    def _call(self, payload: dict, purpose: str, est: int) -> dict:
        max_out = payload[self.cfg.provider.max_tokens_param]
        for attempt in range(1, self.cfg.provider.max_retries + 2):
            self._check_budget(est, max_out)
            self.totals.attempts += 1
            if purpose == "summary" and attempt == 1:
                self.totals.summary_requests += 1
            self.rec.event("model_request", purpose=purpose, attempt=attempt, agent_request=self.totals.agent_requests,
                           estimate_input_tokens=est, payload=payload,
                           **({"wire_payload": self.client.wire(payload)} if hasattr(self.client, "wire") else {}))
            t0 = time.monotonic()
            try:
                resp = self.client.complete(payload)
            except ProviderError as e:
                self.totals.failed_attempts += 1
                self.totals.unreported_tokens_upper_bound += math.ceil(est * 1.5) + max_out
                worst = self.cfg.budget.worst_case(est, max_out)
                if worst is not None:
                    self.totals.cost_upper_bound_usd += worst
                self.rec.event("model_error", purpose=purpose, attempt=attempt, error=e.to_dict(),
                               duration_s=round(time.monotonic() - t0, 3))
                if e.retryable and attempt <= self.cfg.provider.max_retries:
                    continue
                raise Stop("provider_error", f"{e.kind}: {e.message[:300]}") from None
            self.rec.event("model_response", purpose=purpose, attempt=attempt, response=resp,
                           duration_s=round(time.monotonic() - t0, 3))
            self._account(resp, purpose, est)
            return resp
        raise AssertionError("unreachable")

    def _account(self, resp: dict, purpose: str, est: int) -> None:
        u = resp.get("usage")
        if not u or u.get("prompt_tokens") is None or u.get("completion_tokens") is None:
            if self.cfg.provider.require_usage:
                raise Stop("usage_unavailable", f"{purpose} response had no usage")
            u = {"prompt_tokens": est, "completion_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
        t = self.totals
        t.prompt_tokens += u["prompt_tokens"]
        t.completion_tokens += u["completion_tokens"]
        t.cached_tokens += u.get("cached_tokens") or 0
        t.reasoning_tokens += u.get("reasoning_tokens") or 0
        if purpose == "summary":
            t.summary_prompt_tokens += u["prompt_tokens"]
            t.summary_completion_tokens += u["completion_tokens"]
        cost = self.cfg.budget.cost(u["prompt_tokens"], u["completion_tokens"], u.get("cached_tokens") or 0)
        if cost is not None:
            t.cost_reported_usd += cost
            t.cost_upper_bound_usd += cost

    def _check_budget(self, est: int, max_out: int) -> None:
        t, l, b = self.totals, self.cfg.limits, self.cfg.budget
        used = t.prompt_tokens + t.completion_tokens + t.unreported_tokens_upper_bound
        nxt = math.ceil(est * 1.5) + max_out
        if used + nxt > l.max_total_tokens:
            raise Stop("budget_tokens", f"used {used} + next worst case {nxt} > {l.max_total_tokens}")
        worst = b.worst_case(est, max_out)
        if worst is not None and t.cost_upper_bound_usd + worst > self.cost_cap_usd:
            raise Stop("budget_cost", f"spent upper bound ${t.cost_upper_bound_usd:.4f} + next worst case "
                                      f"${worst:.4f} > cap ${self.cost_cap_usd:.4f}")


def initial_state(condition: str, store: ArtifactStore, focused_excerpts: list[dict] | None = None
                  ) -> ConversationState:
    if condition == "natural":
        return ConversationState(pinned=[{"role": "system", "content": prompts.AGENT_SYSTEM},
                                         {"role": "user", "content": prompts.TASK}])
    if condition == "focused":
        parts = []
        for ex in focused_excerpts or []:
            a = store.artifacts[ex["artifact"]]
            parts.append(f"### {a.id} ({a.title})")
            for first, last in ex["lines"]:
                parts.append(store.lines_text(a.id, first, last))
                parts.append("...")
        return ConversationState(pinned=[{"role": "system", "content": prompts.AGENT_SYSTEM},
                                         {"role": "user", "content": prompts.FOCUSED_USER.format(
                                             task=prompts.TASK, excerpts="\n".join(parts))}])
    raise ValueError(f"unknown condition {condition!r}")
