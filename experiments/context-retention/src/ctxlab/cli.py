"""Command line. Only ``ctxlab live ...`` can contact a model, and only with a passing preflight and
``--confirm-spend``. Everything else is offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .paths import EXPERIMENT_DIR, FIXTURE_DIR, GOLD_DIR, RUNS_DIR


def _cfg(path: str):
    from .config import RunConfig
    return RunConfig.load(Path(path))


def cmd_fixture(args) -> int:
    from . import fixture_checks, fixture_gen
    if args.action == "generate":
        fixture_gen.write(fixture_gen.build(), FIXTURE_DIR, GOLD_DIR)
        print(f"wrote {FIXTURE_DIR.relative_to(EXPERIMENT_DIR)} and {GOLD_DIR.relative_to(EXPERIMENT_DIR)}")
        return 0
    problems = fixture_checks.run_all()
    for p in problems:
        print(f"FAIL {p}")
    print("fixture OK: regenerates byte-identically, sources agree, no answer leaks" if not problems
          else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_tools(args) -> int:
    from .artifacts import ArtifactStore
    store = ArtifactStore(FIXTURE_DIR)
    if args.action == "list":
        out = store.list_artifacts()
    elif args.action == "search":
        out = store.search_artifacts(" ".join(args.query))
    else:
        out = store.read_artifact(args.id, args.offset, args.limit)
    print(out.text)
    return 0 if out.ok else 1


def cmd_preflight(args) -> int:
    from .config import preflight, worst_case_bound
    cfg = _cfg(args.config)
    errors, notes = preflight(cfg, args.condition)
    print(f"preflight for condition={args.condition} model={cfg.provider.model or '(unset)'} "
          f"endpoint={cfg.provider.base_url} stream={cfg.provider.stream}")
    for n in notes:
        print(f"NOTE  {n}")
    print("bounds:", json.dumps(worst_case_bound(cfg, args.condition)))
    from .experiment import LedgerError, code_provenance, pilot_ledger
    try:
        print("pilot ledger:", json.dumps(pilot_ledger(cfg)))
    except LedgerError as e:
        errors.append(str(e))
    print("code:", json.dumps(code_provenance()))
    for e in errors:
        print(f"ERROR {e}")
    print("preflight OK (no request was sent)" if not errors else "preflight FAILED: live runs are blocked")
    return 1 if errors else 0


def _live_client(cfg, args):
    from .client import OpenAIChatClient, OpenAIResponsesClient, ProviderError
    from .config import preflight
    condition = getattr(args, "condition", "natural")
    errors, notes = preflight(cfg, condition)
    if errors:
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        print("preflight failed; nothing was sent", file=sys.stderr)
        return None
    if not args.confirm_spend:
        print("refusing to call a paid model without --confirm-spend", file=sys.stderr)
        return None
    for n in notes:
        print(f"NOTE  {n}")
    try:
        cls = OpenAIResponsesClient if cfg.provider.integration == "openai-responses" else OpenAIChatClient
        return cls(cfg.provider.base_url, cfg.provider.api_key_env, cfg.provider.request_timeout_s)
    except ProviderError as e:
        print(f"ERROR {e.message}", file=sys.stderr)
        return None


def cmd_live(args) -> int:
    from .experiment import continuation_state, start_run
    cfg = _cfg(args.config)
    if args.action == "continue":
        args.condition = "natural"  # continuation applies the natural-condition compaction policy
    client = _live_client(cfg, args)
    if client is None:
        return 2
    if args.action == "probe":
        from .probe import probe
        return probe(cfg, client)
    from .experiment import LedgerError
    try:
        if args.action == "run":
            run_dir, result = start_run(cfg, client, args.condition, "real", secrets_=[client.secret])
        else:
            state, parent = continuation_state(Path(args.from_run), args.at_request)
            run_dir, result = start_run(cfg, client, "continuation", "real", state=state, parent=parent,
                                        secrets_=[client.secret])
    except LedgerError as e:
        print(f"ERROR {e}; nothing was sent", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    print(f"recorded {run_dir}")
    print(f"next: uv run ctxlab inspect {run_dir.relative_to(EXPERIMENT_DIR)} && "
          f"uv run ctxlab score {run_dir.relative_to(EXPERIMENT_DIR)}")
    return 0


def cmd_replay(args) -> int:
    from .experiment import replay
    out = replay(Path(args.run))
    print(json.dumps(out, indent=2))
    return 0 if out["ok"] else 1


def cmd_inspect(args) -> int:
    from .audit import analyze, render
    a = analyze(Path(args.run))
    if args.json:
        print(json.dumps(a, indent=2))
    else:
        print(render(a))
    if args.write:
        with open(Path(args.run) / "audit.json", "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(a, indent=2) + "\n")
    return 0


def cmd_score(args) -> int:
    from .scoring import score
    print(json.dumps(score(Path(args.run)), indent=2))
    print(f"manual verdicts go in {Path(args.run) / 'review.json'} (never overwritten by this command)")
    return 0


def cmd_report(args) -> int:
    from .report import build
    print(build(Path(args.runs)))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ctxlab", description="Context retention failure Lab")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fixture", help="generate or verify the incident bundle (offline)")
    f.add_argument("action", choices=["generate", "verify"])
    f.set_defaults(func=cmd_fixture)

    t = sub.add_parser("tools", help="call the agent's tools yourself (offline)")
    tsub = t.add_subparsers(dest="action", required=True)
    tsub.add_parser("list")
    ts = tsub.add_parser("search")
    ts.add_argument("query", nargs="+")
    tr = tsub.add_parser("read")
    tr.add_argument("id")
    tr.add_argument("--offset", type=int, default=1)
    tr.add_argument("--limit", type=int, default=100)
    t.set_defaults(func=cmd_tools)

    pf = sub.add_parser("preflight", help="validate a live config without sending anything")
    pf.add_argument("--config", required=True)
    pf.add_argument("--condition", choices=["natural", "focused"], default="natural")
    pf.set_defaults(func=cmd_preflight)

    lv = sub.add_parser("live", help="PAID: run against a real model")
    lsub = lv.add_subparsers(dest="action", required=True)
    lr = lsub.add_parser("run")
    lr.add_argument("--condition", choices=["natural", "focused"], required=True)
    lc = lsub.add_parser("continue", help="continue from a recorded agent request (frozen transcript)")
    lc.add_argument("--from", dest="from_run", required=True, help="recorded run directory")
    lc.add_argument("--at-request", type=int, required=True, help="1-based agent request number")
    lsub.add_parser("probe", help="two small requests: does the endpoint do tool calls and report usage?")
    for sp in (lr, lc, lsub.choices["probe"]):
        sp.add_argument("--config", required=True)
        sp.add_argument("--confirm-spend", action="store_true")
    lv.set_defaults(func=cmd_live)

    rp = sub.add_parser("replay", help="re-run a recording offline and require identical payloads")
    rp.add_argument("run")
    rp.set_defaults(func=cmd_replay)

    ins = sub.add_parser("inspect", help="evidence audit of a recorded run (offline)")
    ins.add_argument("run")
    ins.add_argument("--json", action="store_true")
    ins.add_argument("--write", action="store_true", help="also write audit.json into the run directory")
    ins.set_defaults(func=cmd_inspect)

    sc = sub.add_parser("score", help="rubric indicators + manual review template (offline)")
    sc.add_argument("run")
    sc.set_defaults(func=cmd_score)

    rpt = sub.add_parser("report", help="aggregate real runs into REPORT.md (offline)")
    rpt.add_argument("--runs", default=str(RUNS_DIR))
    rpt.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
