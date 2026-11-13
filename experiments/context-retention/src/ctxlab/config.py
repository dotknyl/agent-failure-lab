"""Run configuration (TOML) and the preflight that must pass before any live request.

An API key in the environment is not spending authorization. A live run needs a filled config (model, native
context window, threshold, caps, pricing or an explicit no-dollar-cap decision, and who authorized the spend)
and the ``--confirm-spend`` flag on the command line.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


INTEGRATIONS = ("openai-chat-completions", "openai-responses")  # responses: stateless, store=false
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")  # openai 3.50.0 ReasoningEffort


@dataclass
class ProviderConfig:
    integration: str = "openai-chat-completions"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = ""
    stream: bool = False
    max_tokens_param: str = "max_completion_tokens"  # some endpoints only accept "max_tokens"
    temperature: float | None = None  # omitted from requests unless set
    # Chat Completions reasoning_effort, omitted unless set. Some models refuse function tools on Chat Completions
    # unless it is "none" (gpt-5.6-luna, probe 2026-11-13).
    reasoning_effort: str | None = None
    request_timeout_s: float = 180.0
    max_retries: int = 1  # our own retries; SDK retries are always disabled
    require_usage: bool = True


@dataclass
class ContextConfig:
    native_context_tokens: int = 0  # the model's documented window, recorded for disclosure
    compaction_threshold_tokens: int = 0  # configured trigger (estimate + output reserve)
    threshold_is_stress_setting: bool = True
    max_output_tokens: int = 4_000
    summary_max_output_tokens: int = 2_000
    summary_max_words: int = 900
    keep_recent_groups: int = 2
    retain_budget_tokens: int = 6_000
    chars_per_token: float = 3.0
    compaction_enabled: bool = True


@dataclass
class LimitsConfig:
    max_agent_requests: int = 40
    max_tool_calls: int = 80
    max_compactions: int = 6
    max_total_tokens: int = 2_000_000  # provider-reported, all calls incl. summaries and failed attempts' estimate


@dataclass
class BudgetConfig:
    pilot_budget_usd: float = 0.0  # total for the whole pilot: probe + every run (agent, summary, retries)
    max_cost_usd: float = 0.0  # cap for any single invocation; the effective cap is min(this, pilot remaining)
    allow_without_dollar_cap: bool = False
    input_usd_per_mtok: float = 0.0
    cached_input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float = 0.0
    pricing_source: str = ""
    pricing_date: str = ""
    authorized_by: str = ""

    @property
    def priced(self) -> bool:
        return self.input_usd_per_mtok > 0 and self.output_usd_per_mtok > 0

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float | None:
        if not self.priced:
            return None
        cached_price = self.cached_input_usd_per_mtok if self.cached_input_usd_per_mtok is not None \
            else self.input_usd_per_mtok
        return ((prompt_tokens - cached_tokens) * self.input_usd_per_mtok + cached_tokens * cached_price
                + completion_tokens * self.output_usd_per_mtok) / 1e6

    def worst_case(self, est_input_tokens: int, max_output_tokens: int, input_safety: float = 1.5) -> float | None:
        """Upper-bound cost of one request: estimated input inflated by ``input_safety``, full output."""
        if not self.priced:
            return None
        return (math.ceil(est_input_tokens * input_safety) * self.input_usd_per_mtok
                + max_output_tokens * self.output_usd_per_mtok) / 1e6


@dataclass
class RunConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunConfig":
        return cls(
            provider=ProviderConfig(**d.get("provider", {})),
            context=ContextConfig(**d.get("context", {})),
            limits=LimitsConfig(**d.get("limits", {})),
            budget=BudgetConfig(**d.get("budget", {})),
        )

    @classmethod
    def load(cls, path: Path) -> "RunConfig":
        with open(path, "rb") as f:
            return cls.from_dict(tomllib.load(f))


def preflight(cfg: RunConfig, condition: str) -> tuple[list[str], list[str]]:
    """Return (errors, notes). Any error blocks a live run. Makes no network request."""
    errors: list[str] = []
    notes: list[str] = []
    p, c, l, b = cfg.provider, cfg.context, cfg.limits, cfg.budget
    if p.integration not in INTEGRATIONS:
        errors.append(f"provider.integration must be one of {INTEGRATIONS}")
    if p.integration == "openai-responses" and p.stream:
        errors.append("provider.stream is not implemented for the openai-responses integration")
    if not p.model:
        errors.append("provider.model is required")
    if p.max_tokens_param not in ("max_completion_tokens", "max_tokens"):
        errors.append("provider.max_tokens_param must be 'max_completion_tokens' or 'max_tokens'")
    if p.reasoning_effort is not None and p.reasoning_effort not in REASONING_EFFORTS:
        errors.append(f"provider.reasoning_effort must be one of {REASONING_EFFORTS}")
    if p.max_retries < 0 or p.max_retries > 3:
        errors.append("provider.max_retries must be between 0 and 3")
    if c.native_context_tokens <= 0:
        errors.append("context.native_context_tokens is required (the model's documented context window)")
    if condition == "natural" and c.compaction_enabled:
        if c.compaction_threshold_tokens <= 0:
            errors.append("context.compaction_threshold_tokens is required for the natural condition")
        elif c.compaction_threshold_tokens > c.native_context_tokens:
            errors.append("context.compaction_threshold_tokens cannot exceed native_context_tokens")
        if c.compaction_threshold_tokens and c.retain_budget_tokens >= c.compaction_threshold_tokens // 2:
            errors.append("context.retain_budget_tokens should be under half the threshold, or compaction "
                          "cannot reduce the context meaningfully")
        if c.compaction_threshold_tokens and c.compaction_threshold_tokens < c.native_context_tokens:
            notes.append(f"Compaction triggers at an estimated {c.compaction_threshold_tokens} tokens (including a "
                         f"{c.max_output_tokens}-token output reserve); the model's native window is "
                         f"{c.native_context_tokens}. "
                         + ("This is a disclosed stress setting, not the model's limit."
                            if c.threshold_is_stress_setting else ""))
    if l.max_agent_requests < 1 or l.max_tool_calls < 1:
        errors.append("limits must be positive")
    if b.priced:
        if b.max_cost_usd <= 0:
            errors.append("budget.max_cost_usd is required when pricing is set")
        if b.pilot_budget_usd <= 0:
            errors.append("budget.pilot_budget_usd is required when pricing is set (total for probe + all runs)")
        elif b.max_cost_usd > b.pilot_budget_usd:
            errors.append("budget.max_cost_usd cannot exceed budget.pilot_budget_usd")
        if not b.pricing_source or not b.pricing_date:
            errors.append("budget.pricing_source and budget.pricing_date are required with pricing")
    else:
        if not b.allow_without_dollar_cap:
            errors.append("no pricing configured: set budget prices and max_cost_usd, or set "
                          "budget.allow_without_dollar_cap = true and rely on token/request caps")
        else:
            notes.append("No dollar cap: spending is bounded only by limits.max_total_tokens and request limits. "
                         "Cost will be reported as unknown.")
    if not b.authorized_by:
        errors.append("budget.authorized_by is required (who authorized this spend)")
    return errors, notes


def worst_case_bound(cfg: RunConfig, condition: str) -> dict:
    """A rough upper bound on requests and tokens for the configured limits (for the preflight printout)."""
    c, l = cfg.context, cfg.limits
    per_request_input = c.compaction_threshold_tokens if condition == "natural" and c.compaction_enabled \
        else c.native_context_tokens
    agent_requests = 1 if condition == "focused" else l.max_agent_requests
    summary_requests = l.max_compactions if condition == "natural" and c.compaction_enabled else 0
    attempts = (agent_requests + summary_requests) * (1 + cfg.provider.max_retries)
    tokens = attempts * (per_request_input + c.max_output_tokens)
    per_request = cfg.budget.worst_case(per_request_input, c.max_output_tokens, input_safety=1.0)
    return {
        "max_requests_including_retries": attempts,
        "max_tokens_by_request_limits": tokens,
        "max_tokens_cap": l.max_total_tokens,
        "worst_case_usd_by_request_limits": None if per_request is None else round(per_request * attempts, 4),
        "enforced_dollar_cap": cfg.budget.max_cost_usd if cfg.budget.priced else None,
    }
