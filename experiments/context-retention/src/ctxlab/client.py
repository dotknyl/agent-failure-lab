"""The single provider integration: OpenAI Chat Completions (``openai`` SDK), plus the replay client.

Why Chat Completions: it is stateless, so the payload this runner sends *is* the model's context. There is no
server-side conversation (``previous_response_id``/``conversation_id``) or provider-native compaction whose
representation the Lab could not inspect. The base URL is configurable, but an "OpenAI-compatible" endpoint is
not assumed to support tool calling, usage reporting or streaming usage: ``ctxlab live probe`` checks that.

SDK retries are disabled (``max_retries=0``); the runner retries explicitly and records every attempt.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ProviderError(Exception):
    kind: str
    message: str
    status: int | None = None
    retryable: bool = False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "status": self.status, "retryable": self.retryable}


class ChatClient(Protocol):
    def complete(self, payload: dict) -> dict:
        """Send ``payload`` and return a normalized response dict, or raise ProviderError."""


def normalize(raw: dict) -> dict:
    """Normalize a Chat Completions response (as a plain dict) to what the runner needs."""
    choice = (raw.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = raw.get("usage")
    norm_usage = None
    if usage:
        details = usage.get("prompt_tokens_details") or {}
        cdetails = usage.get("completion_tokens_details") or {}
        norm_usage = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cached_tokens": details.get("cached_tokens") or 0,
            "reasoning_tokens": cdetails.get("reasoning_tokens") or 0,
        }
    calls = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
              "arguments": (c.get("function") or {}).get("arguments") or ""}
             for c in msg.get("tool_calls") or []]
    return {"id": raw.get("id"), "model": raw.get("model"), "content": msg.get("content"), "tool_calls": calls,
            "finish_reason": choice.get("finish_reason"), "usage": norm_usage}


class OpenAIChatClient:
    def __init__(self, base_url: str, api_key_env: str, timeout_s: float, http_client=None):
        import openai  # imported lazily: tests, replay and imports never need it

        key = os.environ.get(api_key_env)
        if not key:
            raise ProviderError("configuration", f"environment variable {api_key_env} is not set")
        self.secret = key
        self._openai = openai
        # http_client is only injected by offline tests (a mock transport); live runs use the SDK default.
        self.client = openai.OpenAI(base_url=base_url, api_key=key, max_retries=0, timeout=timeout_s,
                                    **({"http_client": http_client} if http_client is not None else {}))

    def complete(self, payload: dict) -> dict:
        return self._guard(lambda: self._chat(payload))

    def _chat(self, payload: dict) -> dict:
        if payload.get("stream"):
            # Plain chunk accumulation, not the SDK's parsing stream helper: that helper raises on
            # finish_reason "length"/"content_filter" and would lose a truncated answer or summary.
            with self.client.chat.completions.create(**payload) as stream:
                raw = accumulate_stream(stream)
        else:
            raw = self.client.chat.completions.create(**payload).model_dump(mode="json", exclude_none=True)
        out = normalize(raw)
        out["raw"] = raw
        return out

    def _guard(self, call):
        o = self._openai
        try:
            return call()
        except o.APITimeoutError as e:
            raise ProviderError("timeout", self._clean(str(e)), None, True) from None
        except o.APIConnectionError as e:
            raise ProviderError("connection", self._clean(str(e)), None, True) from None
        except o.APIStatusError as e:
            retry = e.status_code in (408, 409, 429, 500, 502, 503, 504)
            raise ProviderError("http_status", self._clean(str(e))[:2000], e.status_code, retry) from None
        except o.OpenAIError as e:
            raise ProviderError("sdk", self._clean(str(e))[:2000], None, False) from None

    def _clean(self, text: str) -> str:
        return text.replace(self.secret, "[REDACTED]") if self.secret else text


def accumulate_stream(chunks) -> dict:
    """Fold Chat Completions stream chunks into one completion-shaped dict (choice 0 only).

    Content and tool-call argument fragments are concatenated in order; tool calls are keyed by their stream
    index. finish_reason and usage are taken as sent (usage arrives in a final chunk when
    stream_options.include_usage is set; if it never arrives, usage stays absent).
    """
    content: list[str] = []
    calls: dict[int, dict] = {}
    finish = None
    usage = None
    head: dict = {}
    n = 0
    for chunk in chunks:
        n += 1
        d = chunk if isinstance(chunk, dict) else chunk.model_dump(mode="json", exclude_none=True)
        for k in ("id", "model", "created"):
            if k in d and k not in head:
                head[k] = d[k]
        if d.get("usage"):
            usage = d["usage"]
        for ch in d.get("choices") or []:
            if ch.get("index", 0) != 0:
                continue
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": None, "type": "function",
                                                             "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    message: dict = {"role": "assistant", "content": "".join(content) or None}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    raw = {**head, "object": "chat.completion", "accumulated_from_stream_chunks": n,
           "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage:
        raw["usage"] = usage
    return raw


def to_responses_wire(payload: dict) -> dict:
    """Translate the runner's (Chat Completions-shaped) payload into a stateless Responses API request.

    store=False and the full history every time: no server-side state. Assistant messages may carry
    ``reasoning_items`` (opaque encrypted reasoning returned by an earlier response in this run); they are passed
    back unchanged, before that turn's message and function calls, as OpenAI recommends for tool-call
    continuations. Other runner-only keys (``reasoning_tokens_reported``) are not sent.
    """
    items: list[dict] = []
    for m in payload["messages"]:
        role = m["role"]
        if role in ("system", "user"):
            items.append({"role": role, "content": m["content"]})
        elif role == "assistant":
            items.extend(m.get("reasoning_items") or [])
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for c in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": c["id"], "name": c["function"]["name"],
                              "arguments": c["function"]["arguments"]})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
    wire: dict = {"model": payload["model"], "input": items, "store": False}
    if payload.get("tools"):
        wire["tools"] = [{"type": "function", "name": t["function"]["name"],
                          "description": t["function"]["description"],
                          "parameters": t["function"]["parameters"], "strict": False} for t in payload["tools"]]
    out_cap = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if out_cap is not None:
        wire["max_output_tokens"] = out_cap
    if payload.get("reasoning_effort") is not None:
        wire["reasoning"] = {"effort": payload["reasoning_effort"]}
        wire["include"] = ["reasoning.encrypted_content"]
    if payload.get("temperature") is not None:
        wire["temperature"] = payload["temperature"]
    return wire


def normalize_responses(raw: dict) -> dict:
    """Normalize a Responses API result to the runner's shape. Reasoning items are kept as opaque dicts."""
    content, calls, reasoning = [], [], []
    for item in raw.get("output") or []:
        t = item.get("type")
        if t == "reasoning":
            reasoning.append(item)
        elif t == "function_call":
            calls.append({"id": item.get("call_id"), "name": item.get("name"), "arguments": item.get("arguments") or ""})
        elif t == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    content.append(part.get("text") or "")
    if raw.get("status") == "incomplete":
        reason = (raw.get("incomplete_details") or {}).get("reason")
        finish = {"max_output_tokens": "length"}.get(reason, reason or "incomplete")
    else:
        finish = "tool_calls" if calls else "stop"
    usage = raw.get("usage")
    norm_usage = None
    if usage:
        norm_usage = {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0,
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
        }
    return {"id": raw.get("id"), "model": raw.get("model"), "content": "".join(content) or None,
            "tool_calls": calls, "reasoning_items": reasoning, "finish_reason": finish, "usage": norm_usage}


class OpenAIResponsesClient(OpenAIChatClient):
    """Responses API, stateless (store=False), non-streamed. Same key handling, retries policy and error mapping."""

    def wire(self, payload: dict) -> dict:
        return to_responses_wire(payload)

    def complete(self, payload: dict) -> dict:
        if payload.get("stream"):
            raise ProviderError("configuration", "streaming is not implemented for the Responses integration")
        return self._guard(lambda: self._responses(payload))

    def _responses(self, payload: dict) -> dict:
        raw = self.client.responses.create(**to_responses_wire(payload)).model_dump(mode="json", exclude_none=True)
        out = normalize_responses(raw)
        out["raw"] = raw
        return out


class ReplayMismatch(Exception):
    pass


class ReplayClient:
    """Serves recorded responses in order and fails on the first outgoing payload that differs from the recording."""

    def __init__(self, attempts: list[dict]):
        self.attempts = attempts  # [{"payload":..., "response": {...}} | {"payload":..., "error": {...}}]
        self.index = 0

    def complete(self, payload: dict) -> dict:
        if self.index >= len(self.attempts):
            raise ReplayMismatch(f"the runner sent request #{self.index + 1}, but only {len(self.attempts)} were "
                                 "recorded")
        rec = self.attempts[self.index]
        self.index += 1
        if canonical(rec["payload"]) != canonical(payload):
            raise ReplayMismatch(f"request #{self.index} differs from the recording:\n"
                                 + first_difference(rec["payload"], payload))
        if "error" in rec:
            e = rec["error"]
            raise ProviderError(e["kind"], e["message"], e.get("status"), e.get("retryable", False))
        return rec["response"]

    @property
    def exhausted(self) -> bool:
        return self.index == len(self.attempts)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def first_difference(a, b, path: str = "$") -> str:
    if type(a) is not type(b):
        return f"{path}: type {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: present only in {'recording' if k in a else 'replay'}"
            if canonical(a[k]) != canonical(b[k]):
                return first_difference(a[k], b[k], f"{path}.{k}")
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if canonical(x) != canonical(y):
                return first_difference(x, y, f"{path}[{i}]")
    return f"{path}: {str(a)[:200]!r} != {str(b)[:200]!r}"
