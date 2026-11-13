"""Offline, provider-independent token estimate.

This is deliberately a character heuristic: it needs no tokenizer download and no network, and the runner
records it next to the provider-reported ``prompt_tokens`` for every request so the error is visible. It is an
estimate, not usage. The default of 3.0 characters per token is meant to over-count JSON-heavy logs and traces;
check the recorded estimate/usage ratio after a pilot and adjust ``chars_per_token`` if it is badly off.
"""

from __future__ import annotations

import json
import math

PER_MESSAGE_OVERHEAD = 4
PER_REQUEST_OVERHEAD = 8


def estimate_text(text: str | None, chars_per_token: float) -> int:
    return math.ceil(len(text or "") / chars_per_token)


def estimate_message(message: dict, chars_per_token: float) -> int:
    n = PER_MESSAGE_OVERHEAD + estimate_text(message.get("content") or "", chars_per_token)
    for call in message.get("tool_calls") or []:
        fn = call.get("function", {})
        n += estimate_text(fn.get("name", "") + fn.get("arguments", "") + call.get("id", ""), chars_per_token) + 4
    if message.get("tool_call_id"):
        n += estimate_text(message["tool_call_id"], chars_per_token)
    if message.get("reasoning_items"):
        # Opaque encrypted reasoning carried back (Responses API): counted by the provider-reported reasoning
        # tokens of the response that produced it, not by the length of the encrypted blob.
        n += message.get("reasoning_tokens_reported") or 0
    return n


def estimate_messages(messages: list[dict], chars_per_token: float) -> int:
    return sum(estimate_message(m, chars_per_token) for m in messages)


def estimate_payload(payload: dict, chars_per_token: float) -> int:
    """Estimated input tokens of a Chat Completions payload: instructions, tool schemas and history."""
    n = PER_REQUEST_OVERHEAD + estimate_messages(payload.get("messages", []), chars_per_token)
    if payload.get("tools"):
        n += estimate_text(json.dumps(payload["tools"], separators=(",", ":")), chars_per_token)
    return n
