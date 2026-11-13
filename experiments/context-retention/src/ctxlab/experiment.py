"""Wiring for live runs, continuations from frozen transcripts, and offline replay of recorded runs."""

from __future__ import annotations

import hashlib
import json
import platform
import secrets
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from . import prompts
from .artifacts import TOOL_SCHEMAS, ArtifactStore
from .client import ReplayClient, ReplayMismatch, canonical
from .config import RunConfig
from .history import ConversationState
from .paths import EXPERIMENT_DIR, FIXTURE_DIR, GOLD_DIR, RUNS_DIR
from .runner import Runner, initial_state
from .trace import MemoryRecorder, Recorder, load_events, load_manifest


def new_run_id(condition: str) -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{condition}-{secrets.token_hex(3)}"


def versions() -> dict:
    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for pkg in ("openai", "ctxlab"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = None
    return out


def code_provenance() -> dict:
    """Hashes that tie a recording to the exact code and lockfile, with or without a git commit."""
    files = sorted((EXPERIMENT_DIR / "src" / "ctxlab").glob("*.py"))
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    out = {"src_sha256": h.hexdigest(), "src_files": len(files)}
    for name in ("pyproject.toml", "uv.lock"):
        p = EXPERIMENT_DIR / name
        out[f"{name}_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    try:
        import subprocess
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=EXPERIMENT_DIR, capture_output=True, text=True,
                              timeout=10)
        out["git_head"] = head.stdout.strip() if head.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        out["git_head"] = None
    return out


class LedgerError(Exception):
    pass


def pilot_ledger(cfg: RunConfig, runs_dir: Path = RUNS_DIR) -> dict:
    """Spending recorded so far by real runs and probes, and the effective cap for the next invocation.

    Every recorded run counts (failed and limit-stopped runs included), using its cost upper bound. An earlier
    run whose cost is unknown makes the remaining budget unknowable, so it is an error while a pilot budget is set.
    """
    b = cfg.budget
    spent, counted, unknown = 0.0, [], []
    runs_dir = Path(runs_dir)
    candidates = [*runs_dir.glob("*/manifest.json"), *runs_dir.glob("_probes/*/manifest.json")]
    for mf in sorted(candidates):
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        if manifest.get("provenance") not in ("real", "probe"):
            continue
        res = mf.parent / "result.json"
        totals = json.loads(res.read_text(encoding="utf-8")).get("totals") if res.exists() else None
        cost = (totals or {}).get("cost_upper_bound_usd")
        if cost is None:
            unknown.append(mf.parent.name)
        else:
            spent += cost
            counted.append(mf.parent.name)
    ledger = {"pilot_budget_usd": b.pilot_budget_usd or None, "max_cost_usd": b.max_cost_usd,
              "spent_upper_bound_usd": round(spent, 6), "counted": counted, "unknown_cost": unknown}
    if not b.priced:
        ledger["effective_cap_usd"] = None
        return ledger
    if b.pilot_budget_usd > 0 and unknown:
        raise LedgerError(f"recorded runs with unknown cost {unknown}: the remaining pilot budget cannot be computed "
                          "(an interrupted run has no result.json; review it and record its cost before continuing)")
    remaining = b.pilot_budget_usd - spent if b.pilot_budget_usd > 0 else b.max_cost_usd
    ledger["remaining_usd"] = round(remaining, 6)
    ledger["effective_cap_usd"] = round(max(0.0, min(b.max_cost_usd, remaining)), 6)
    return ledger


def build_manifest(run_id: str, cfg: RunConfig, store: ArtifactStore, condition: str, provenance: str,
                   parent: dict | None = None, ledger: dict | None = None) -> dict:
    return {
        "code": code_provenance(),
        "config_sha256": hashlib.sha256(canonical(cfg.to_dict()).encode()).hexdigest(),
        "budget_ledger": ledger,
        "run_id": run_id,
        "provenance": provenance,  # "real" = responses from a live model; "mock" = scripted (tests only)
        "condition": condition,
        "parent": parent,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fixture": {"id": store.fixture_id, "bundle_sha256": store.bundle_sha256,
                    "artifacts": {a.id: a.sha256 for a in store.artifacts.values()}},
        "prompts": {"version": prompts.PROMPT_VERSION, "sha256": prompts.prompt_hashes()},
        "tool_schemas_sha256": hashlib.sha256(canonical(TOOL_SCHEMAS).encode()).hexdigest(),
        "config": cfg.to_dict(),  # contains the API key's environment variable name, never the key
        "versions": versions(),
        "disclosures": {
            "native_context_tokens": cfg.context.native_context_tokens,
            "compaction_threshold_tokens": cfg.context.compaction_threshold_tokens,
            "threshold_is_stress_setting": cfg.context.threshold_is_stress_setting,
            "token_estimate": f"character heuristic, {cfg.context.chars_per_token} chars/token; compare with "
                              "provider-reported prompt_tokens in model_response events",
            "provider_side": "Only the request payload and response body are observable. Provider-side "
                             "transformations (truncation, caching, routing, hidden system prompts) are not.",
        },
    }


def load_focused() -> list[dict]:
    return json.loads((GOLD_DIR / "focused_excerpts.json").read_text(encoding="utf-8"))


def start_run(cfg: RunConfig, client, condition: str, provenance: str, runs_dir: Path = RUNS_DIR,
              state: ConversationState | None = None, parent: dict | None = None,
              secrets_: list[str] | None = None) -> tuple[Path, dict]:
    store = ArtifactStore(FIXTURE_DIR)
    ledger = pilot_ledger(cfg, runs_dir)  # raises LedgerError before anything is recorded or sent
    run_id = new_run_id(condition)
    run_dir = Path(runs_dir) / run_id
    rec = Recorder(run_dir, secrets=secrets_)
    manifest = build_manifest(run_id, cfg, store, condition, provenance, parent, ledger)
    rec.write_json("manifest.json", manifest)
    if state is None:
        state = initial_state(condition, store, load_focused() if condition == "focused" else None)
    runner = Runner(store, client, rec, cfg, condition, tools_enabled=condition != "focused",
                    cost_cap_usd=ledger.get("effective_cap_usd"))
    try:
        result = runner.run(state, {"parent": parent} if parent else None)
    finally:
        rec.close()
    result["redactions"] = rec.redactions
    rec.write_json("result.json", result)
    return run_dir, result


def continuation_state(run_dir: Path, at_request: int) -> tuple[ConversationState, dict]:
    """Messages of the recorded agent request number ``at_request`` (1-based), as a frozen transcript."""
    reqs = [e for e in load_events(run_dir) if e["type"] == "model_request" and e["purpose"] == "agent"
            and e["attempt"] == 1]
    if not 1 <= at_request <= len(reqs):
        raise ValueError(f"run has {len(reqs)} agent requests; --at-request must be in 1..{len(reqs)}")
    payload = reqs[at_request - 1]["payload"]
    parent = {"run_id": load_manifest(run_dir)["run_id"], "at_agent_request": at_request,
              "payload_sha256": hashlib.sha256(canonical(payload).encode()).hexdigest()}
    return ConversationState.from_messages(payload["messages"]), parent


def replay(run_dir: Path) -> dict:
    """Re-run the recorded run offline: same runner, recorded model responses, fixture tools recomputed.

    Passes only if every outgoing payload is identical to the recording and the run ends the same way. This
    checks the harness and the trace's integrity; it says nothing new about model behaviour.
    """
    manifest = load_manifest(run_dir)
    events = load_events(run_dir)
    store = ArtifactStore(FIXTURE_DIR)
    if store.bundle_sha256 != manifest["fixture"]["bundle_sha256"]:
        return {"ok": False, "reason": "fixture bundle differs from the one this run used"}
    attempts = []
    for e in events:
        if e["type"] == "model_request":
            attempts.append({"payload": e["payload"]})
        elif e["type"] == "model_response":
            attempts[-1]["response"] = e["response"]
        elif e["type"] == "model_error":
            attempts[-1]["error"] = e["error"]
    start = next(e for e in events if e["type"] == "run_started")
    finished = next((e for e in events if e["type"] == "run_finished"), None)
    if finished is None:
        return {"ok": False, "reason": "recording has no run_finished event (interrupted run)"}
    cfg = RunConfig.from_dict(manifest["config"])
    client = ReplayClient(attempts)
    rec = MemoryRecorder()
    state = ConversationState.from_messages(start["initial_messages"])
    runner = Runner(store, client, rec, cfg, manifest["condition"],
                    tools_enabled=start.get("tools_enabled", True),
                    cost_cap_usd=(manifest.get("budget_ledger") or {}).get("effective_cap_usd"))
    try:
        result = runner.run(state)
    except ReplayMismatch as e:
        return {"ok": False, "reason": str(e)}
    problems = []
    if result["stop_reason"] != finished["stop_reason"]:
        problems.append(f"stop reason {result['stop_reason']} != recorded {finished['stop_reason']}")
    if not client.exhausted:
        problems.append(f"only {client.index} of {len(attempts)} recorded requests were replayed")
    recorded_tools = [e["content"] for e in events if e["type"] == "tool_result"]
    replayed_tools = [e["content"] for e in rec.events if e["type"] == "tool_result"]
    if recorded_tools != replayed_tools:
        problems.append("tool results differ from the recording")
    return {"ok": not problems, "reason": "; ".join(problems) or "identical", "requests": len(attempts),
            "stop_reason": result["stop_reason"]}


__all__ = ["start_run", "replay", "continuation_state", "EXPERIMENT_DIR"]
