"""Locally owned conversation state, Chat Completions message validity and compaction planning.

The context sent to the model is always ``pinned + [summary] + body``:

* ``pinned``: the system instructions and the original task. Never compacted, so the task and its safety
  constraints are always present.
* ``summary``: the latest compaction summary (it already folds in any previous summary), or nothing.
* ``body``: complete groups. A group is one assistant message plus the tool results answering every tool call
  in it (or one assistant message without tool calls).

Compaction only ever moves whole groups out of ``body``, so a tool call is never separated from its result.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import prompts
from .tokens import estimate_messages


class InvalidHistory(Exception):
    pass


@dataclass
class ConversationState:
    pinned: list[dict]
    summary: str | None = None
    summary_message: dict | None = None
    body: list[dict] = field(default_factory=list)

    def messages(self) -> list[dict]:
        return [*self.pinned, *([self.summary_message] if self.summary_message else []), *self.body]

    @classmethod
    def from_messages(cls, messages: list[dict]) -> "ConversationState":
        """Rebuild state from a recorded payload (continuation from a frozen transcript)."""
        messages = copy.deepcopy(messages)
        if len(messages) < 2 or messages[0]["role"] != "system" or messages[1]["role"] != "user":
            raise InvalidHistory("expected [system, user task, ...] at the start of the recorded payload")
        state = cls(pinned=messages[:2])
        rest = messages[2:]
        if rest and rest[0]["role"] == "user" and str(rest[0].get("content", "")).startswith(
                prompts.SUMMARY_MESSAGE_PREFIX):
            state.summary_message = rest[0]
            state.summary = rest[0]["content"].split("\n\n", 1)[-1]
            rest = rest[1:]
        state.body = rest
        return state


def validate(messages: list[dict]) -> None:
    """Raise InvalidHistory unless ``messages`` is a sendable Chat Completions history."""
    if not messages or messages[0].get("role") != "system":
        raise InvalidHistory("history must start with the system message")
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "tool":
            cid = m.get("tool_call_id")
            if cid not in pending:
                raise InvalidHistory(f"message {i}: tool result {cid!r} does not answer a pending tool call")
            del pending[cid]
            if not isinstance(m.get("content"), str):
                raise InvalidHistory(f"message {i}: tool result content must be a string")
            continue
        if pending:
            raise InvalidHistory(f"message {i}: tool calls {sorted(pending)} were not answered before a {role} "
                                 "message")
        if role == "assistant":
            calls = m.get("tool_calls") or []
            if not calls and not m.get("content"):
                raise InvalidHistory(f"message {i}: assistant message has neither content nor tool calls")
            for c in calls:
                cid = c.get("id")
                if not cid or cid in seen:
                    raise InvalidHistory(f"message {i}: tool call id {cid!r} is missing or duplicated")
                seen.add(cid)
                pending[cid] = c.get("function", {}).get("name", "")
        elif role not in ("system", "user"):
            raise InvalidHistory(f"message {i}: unexpected role {role!r}")
    if pending:
        raise InvalidHistory(f"history ends with unanswered tool calls {sorted(pending)}")


def groups(body: list[dict]) -> list[list[dict]]:
    """Split ``body`` into complete groups. Raises if a group is incomplete (unanswered tool call)."""
    out: list[list[dict]] = []
    for m in body:
        if m["role"] == "tool":
            if not out:
                raise InvalidHistory("body starts with a tool result")
            out[-1].append(m)
        else:
            out.append([m])
    for g in out:
        head = g[0]
        expected = {c["id"] for c in head.get("tool_calls") or []}
        answered = {m["tool_call_id"] for m in g[1:]}
        if head["role"] == "assistant" and expected != answered:
            raise InvalidHistory(f"incomplete group: calls {sorted(expected)} answered {sorted(answered)}")
    return out


@dataclass
class CompactionPlan:
    to_summarize: list[dict]
    retained: list[dict]
    summarized_groups: int
    retained_groups: int


def plan_compaction(state: ConversationState, keep_recent_groups: int, retain_budget_tokens: int,
                    chars_per_token: float) -> CompactionPlan | None:
    """Choose which complete groups to summarize. Returns None when there is nothing older to summarize.

    Keeps up to ``keep_recent_groups`` most recent groups whose estimated size fits ``retain_budget_tokens``;
    the most recent group is kept even when it alone exceeds the budget (the caller then detects an
    ineffective compaction instead of silently dropping it).
    """
    gs = groups(state.body)
    kept: list[list[dict]] = []
    used = 0
    for g in reversed(gs):
        size = estimate_messages(g, chars_per_token)
        if len(kept) >= keep_recent_groups or (kept and used + size > retain_budget_tokens):
            break
        kept.insert(0, g)
        used += size
    older = gs[:len(gs) - len(kept)]
    if not older:
        return None
    return CompactionPlan(
        to_summarize=[m for g in older for m in g],
        retained=[m for g in kept for m in g],
        summarized_groups=len(older),
        retained_groups=len(kept),
    )


def render_transcript(messages: list[dict]) -> str:
    """Plain-text rendering of messages for the summarizer. Nothing is shortened."""
    out = []
    for m in messages:
        if m["role"] == "assistant":
            if m.get("reasoning_items"):
                out.append(f"[{len(m['reasoning_items'])} encrypted reasoning item(s): opaque, not included]")
            if m.get("content"):
                out.append(f"ASSISTANT: {m['content']}")
            for c in m.get("tool_calls") or []:
                out.append(f"TOOL CALL {c['id']}: {c['function']['name']}({c['function']['arguments']})")
        elif m["role"] == "tool":
            out.append(f"TOOL RESULT {m['tool_call_id']}:\n{m['content']}")
        else:
            out.append(f"{m['role'].upper()}: {m.get('content', '')}")
    return "\n\n".join(out)
