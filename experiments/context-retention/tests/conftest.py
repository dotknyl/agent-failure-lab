"""Test helpers. Tests never contact the network: socket connections are blocked for the whole session.

``ScriptedClient`` is a MOCK model for harness tests only. Its runs are recorded with provenance "mock" and are
refused by the report generator.
"""

from __future__ import annotations

import itertools
import json
import socket
from pathlib import Path

import pytest

from ctxlab.config import RunConfig
from ctxlab.paths import FIXTURE_DIR


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    original = socket.socket.connect

    def guard(*args, **kwargs):
        raise RuntimeError("tests must not open network connections")

    socket.socket.connect = guard
    yield
    socket.socket.connect = original


class ScriptedClient:
    """Returns scripted responses in order. Each step is a response dict, a ProviderError to raise, or a
    callable(payload) -> response dict. Records every payload it receives."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.payloads: list[dict] = []

    def complete(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        if not self.steps:
            raise AssertionError("ScriptedClient ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            step = step(payload)
        return step


def usage(prompt=100, completion=20):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
            "cached_tokens": 0, "reasoning_tokens": 0}


_ids = itertools.count(1)


def tool_calls(*calls, content=None):
    """calls: (name, args_dict) tuples -> scripted assistant response with tool calls (unique ids, like providers)."""
    def make(payload):
        return {"id": "r", "model": "mock", "content": content, "finish_reason": "tool_calls", "usage": usage(),
                "tool_calls": [{"id": f"call_{next(_ids)}", "name": name, "arguments": json.dumps(args)}
                               for name, args in calls]}
    return make


def final(text, finish="stop"):
    return {"id": "r", "model": "mock", "content": text, "tool_calls": [], "finish_reason": finish, "usage": usage()}


def summary(text):
    return {"id": "s", "model": "mock", "content": text, "tool_calls": [], "finish_reason": "stop",
            "usage": usage(500, 100)}


@pytest.fixture
def cfg() -> RunConfig:
    c = RunConfig()
    c.provider.model = "mock-model"
    c.provider.max_retries = 1
    c.context.native_context_tokens = 200_000
    c.context.compaction_threshold_tokens = 12_000
    c.context.max_output_tokens = 1_000
    c.context.summary_max_output_tokens = 500
    c.context.retain_budget_tokens = 4_000
    c.context.keep_recent_groups = 1
    c.budget.allow_without_dollar_cap = True
    c.budget.authorized_by = "test"
    return c


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR
