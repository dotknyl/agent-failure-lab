"""Deterministic generator for the synthetic checkout-latency incident bundle (fixture v1).

Every artifact the agent can read, and every gold fact the rubric uses, is derived from one simulated
population of checkout requests. That is what keeps timestamps, durations, attempt counts, log lines, sampled
traces, per-minute metrics and alerts mutually consistent.

The scenario (gold, never visible to the agent through tools):

* 2026-10-04: the payment gateway degrades (some authorization attempts hang) while checkout-api still runs
  v2.13.3 with payment-client profile v17 (2000 ms timeout, 2 attempts). Impact is bounded.
* 2026-10-06 14:04: checkout-api v2.14.0 ships payment-client profile v18 (8000 ms timeout, 4 attempts,
  exponential backoff). Latency stays normal while the gateway is healthy.
* 2026-10-06 14:10-14:41: scheduled orders-db reindex raises CPU and fires a warning (competing explanation).
  Database spans stay small and latency stays high after the maintenance ends.
* 2026-10-06 14:19-15:04: the same kind of gateway degradation recurs; under v18 each hung attempt waits 8 s
  and is retried up to 4 times, so checkout latency is amplified to tens of seconds.

Run ``uv run ctxlab fixture generate`` to rewrite the checked-in bundle; tests regenerate it into a temporary
directory and require byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FIXTURE_ID = "checkout-latency-v1"
SEED = 20261006
REQUEST_INTERVAL_MS = 5_000
TRACE_SAMPLE_EVERY = 12  # head sampling: 1 in 12 requests -> about one trace per minute
INSTANCES = 3
GATEWAY = "paygw-eu-1"

DEGRADED_TIMEOUT_P = 0.30
DEGRADED_502_P = 0.05
BASE_TIMEOUT_P = 0.0005
BASE_502_P = 0.002

LATENCY_ALERT_MS = 3_000
LATENCY_ALERT_MINUTES = 3
PAYMENT_ERROR_ALERT_RATIO = 0.10
PAYMENT_ERROR_ALERT_MINUTES = 2
DB_CPU_ALERT_PCT = 75
DB_CPU_ALERT_MINUTES = 5


def at(day: str, hms: str) -> int:
    return int(datetime.fromisoformat(f"{day}T{hms}+00:00").timestamp() * 1000)


def iso(t: int) -> str:
    dt = datetime.fromtimestamp(t // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t % 1000:03d}Z"


def iso_s(t: int) -> str:
    return datetime.fromtimestamp(t // 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(t: int) -> str:
    return datetime.fromtimestamp(t // 1000, tz=timezone.utc).strftime("%H:%M")


def minute_of(t: int) -> int:
    return t - t % 60_000


@dataclass(frozen=True)
class PaymentProfile:
    version: int
    timeout_ms: int
    max_attempts: int
    backoff: str
    backoff_base_ms: int

    def backoff_after(self, attempt: int) -> int:
        if self.backoff == "fixed":
            return self.backoff_base_ms
        return self.backoff_base_ms * 2 ** (attempt - 1)


V17 = PaymentProfile(17, 2_000, 2, "fixed", 100)
V18 = PaymentProfile(18, 8_000, 4, "exponential", 250)

DAY_A = "2026-10-04"
DAY_B = "2026-10-06"


@dataclass(frozen=True)
class DayPlan:
    day: str
    start: int
    end: int
    release_at: int | None
    degraded: tuple[int, int]
    db_maintenance: tuple[int, int] | None


PLANS = (
    DayPlan(DAY_A, at(DAY_A, "13:30:00"), at(DAY_A, "15:00:00"), None,
            (at(DAY_A, "14:02:00"), at(DAY_A, "14:24:00")), None),
    DayPlan(DAY_B, at(DAY_B, "13:30:00"), at(DAY_B, "15:45:00"), at(DAY_B, "14:04:00"),
            (at(DAY_B, "14:19:00"), at(DAY_B, "15:04:00")), (at(DAY_B, "14:10:00"), at(DAY_B, "14:41:00"))),
)
DEPLOY_STARTED = at(DAY_B, "14:02:10")


@dataclass
class Span:
    trace: str
    span: str
    parent: str | None
    name: str
    service: str
    instance: str
    start: int
    duration_ms: int
    attrs: dict

    def to_line(self) -> str:
        return json.dumps({
            "trace": self.trace, "span": self.span, "parent": self.parent, "name": self.name,
            "service": self.service, "instance": self.instance, "start": iso(self.start),
            "duration_ms": self.duration_ms, "attrs": self.attrs,
        }, separators=(",", ":"))


@dataclass
class Attempt:
    n: int
    start: int
    duration_ms: int
    outcome: str  # ok | timeout | http_502
    backoff_after_ms: int | None


@dataclass
class Request:
    day: str
    index: int
    arrival: int
    instance: str
    release: str
    profile: PaymentProfile
    trace: str
    order_id: str
    idempotency_key: str
    attempts: list[Attempt] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    status: int = 200
    duration_ms: int = 0
    authorize_end: int = 0
    sampled: bool = False

    @property
    def db_ms(self) -> int:
        return sum(s.duration_ms for s in self.spans if s.service == "orders-db")


def _hex(rnd: random.Random, bits: int) -> str:
    return f"{rnd.getrandbits(bits):0{bits // 4}x}"


def simulate_day(plan: DayPlan, rnd: random.Random) -> list[Request]:
    requests: list[Request] = []
    index = 0
    while True:
        arrival = plan.start + index * REQUEST_INTERVAL_MS + rnd.randint(0, 4_000)
        if arrival >= plan.end:
            break
        released = plan.release_at is not None and arrival >= plan.release_at
        profile = V18 if released else V17
        req = Request(
            day=plan.day, index=index, arrival=arrival, instance=f"checkout-api-{index % INSTANCES}",
            release="2.14.0" if released else "2.13.3", profile=profile, trace=_hex(rnd, 64),
            order_id="ord_" + _hex(rnd, 32), idempotency_key="", sampled=index % TRACE_SAMPLE_EVERY == 0,
        )
        req.idempotency_key = "ik_" + req.order_id[4:]
        _build_spans(req, plan, rnd)
        requests.append(req)
        index += 1
    return requests


def _in(window: tuple[int, int] | None, t: int) -> bool:
    return window is not None and window[0] <= t < window[1]


def _build_spans(req: Request, plan: DayPlan, rnd: random.Random) -> None:
    root_id = _hex(rnd, 32)
    spans: list[Span] = []
    cur = req.arrival + 1

    def child(name: str, service: str, dur: int, attrs: dict, parent: str = root_id) -> Span:
        nonlocal cur
        s = Span(req.trace, _hex(rnd, 32), parent, name, service, req.instance, cur, dur, attrs)
        spans.append(s)
        return s

    maint = _in(plan.db_maintenance, req.arrival)
    s = child("cart.load", "orders-db", rnd.randint(10, 24) if maint else rnd.randint(6, 14),
              {"db.system": "postgresql", "db.operation": "SELECT", "db.table": "carts"})
    cur += s.duration_ms + rnd.randint(1, 2)
    if req.release == "2.14.0":
        s = child("address.validate", "address-svc", rnd.randint(22, 40), {"http.status": 200})
        cur += s.duration_ms + rnd.randint(1, 2)
    s = child("fraud.screen", "fraud-svc", rnd.randint(30, 60), {"http.status": 200, "decision": "allow"})
    cur += s.duration_ms + rnd.randint(1, 2)

    auth_start = cur
    auth = Span(req.trace, _hex(rnd, 32), root_id, "payment.authorize", "checkout-api", req.instance,
                auth_start, 0, {"gateway": GATEWAY, "idempotency_key": req.idempotency_key})
    spans.append(auth)
    p = req.profile
    success = False
    for n in range(1, p.max_attempts + 1):
        degraded = _in(plan.degraded, cur)
        r = rnd.random()
        t_p, e_p = (DEGRADED_TIMEOUT_P, DEGRADED_502_P) if degraded else (BASE_TIMEOUT_P, BASE_502_P)
        if r < t_p:
            outcome, dur = "timeout", p.timeout_ms + rnd.randint(1, 6)
        elif r < t_p + e_p:
            outcome, dur = "http_502", rnd.randint(40, 90)
        else:
            outcome, dur = "ok", rnd.randint(150, 420)
        attrs = {"attempt": n, "max_attempts": p.max_attempts, "outcome": outcome,
                 "timeout_ms": p.timeout_ms, "gateway": GATEWAY, "idempotency_key": req.idempotency_key}
        spans.append(Span(req.trace, _hex(rnd, 32), auth.span, "payment.attempt", "checkout-api", req.instance,
                          cur, dur, attrs))
        cur += dur
        backoff = None
        if outcome == "ok":
            success = True
        elif n < p.max_attempts:
            backoff = p.backoff_after(n)
        req.attempts.append(Attempt(n, cur - dur, dur, outcome, backoff))
        if success:
            break
        if backoff is not None:
            cur += backoff
    cur += 1
    auth.duration_ms = cur - auth_start
    auth.attrs["attempts"] = len(req.attempts)
    auth.attrs["result"] = "authorized" if success else "failed"
    req.authorize_end = cur
    cur += rnd.randint(1, 2)

    if success:
        s = child("order.persist", "orders-db", rnd.randint(12, 30) if maint else rnd.randint(8, 18),
                  {"db.system": "postgresql", "db.operation": "INSERT", "db.table": "orders"})
    else:
        s = child("order.record_failure", "orders-db", rnd.randint(9, 20) if maint else rnd.randint(5, 12),
                  {"db.system": "postgresql", "db.operation": "UPDATE", "db.table": "orders"})
    cur += s.duration_ms + rnd.randint(1, 3)

    req.status = 200 if success else 502
    req.duration_ms = cur - req.arrival
    root = Span(req.trace, root_id, None, "POST /checkout", "checkout-api", req.instance, req.arrival,
                req.duration_ms, {"http.route": "/checkout", "http.method": "POST", "http.status": req.status,
                                  "order_id": req.order_id, "release": req.release})
    req.spans = [root, *spans]


def nearest_rank(values: list[int], q: float) -> int:
    ordered = sorted(values)
    k = max(1, -(-int(q * 1000) * len(ordered) // 1000))  # ceil(q * n) with integer arithmetic
    return ordered[min(k, len(ordered)) - 1]


@dataclass
class MinuteRow:
    minute: int
    requests: int
    http_5xx: int
    p50: int
    p95: int
    p99: int
    release: str
    attempts: int
    failed_attempts: int
    db_p95: int
    cpu_pct: int
    active_connections: int
    pool_wait_p95_ms: int
    replication_lag_ms: int


def minute_rows(plan: DayPlan, reqs: list[Request], rnd: random.Random) -> list[MinuteRow]:
    rows = []
    for m in range(minute_of(plan.start), plan.end, 60_000):
        bucket = [r for r in reqs if minute_of(r.arrival) == m]
        if not bucket:
            continue
        durs = [r.duration_ms for r in bucket]
        db_durs = [s.duration_ms for r in bucket for s in r.spans if s.service == "orders-db"]
        attempts = [a for r in bucket for a in r.attempts]
        maint = _in(plan.db_maintenance, m)
        releases = sorted({r.release for r in bucket})
        rows.append(MinuteRow(
            minute=m, requests=len(bucket), http_5xx=sum(r.status >= 500 for r in bucket),
            p50=nearest_rank(durs, 0.50), p95=nearest_rank(durs, 0.95), p99=nearest_rank(durs, 0.99),
            release="/".join(releases), attempts=len(attempts),
            failed_attempts=sum(a.outcome != "ok" for a in attempts), db_p95=nearest_rank(db_durs, 0.95),
            cpu_pct=rnd.randint(80, 92) if maint else rnd.randint(26, 38),
            active_connections=rnd.randint(14, 22),
            pool_wait_p95_ms=rnd.randint(1, 3) if maint else rnd.randint(0, 1),
            replication_lag_ms=rnd.randint(250, 600) if maint else rnd.randint(80, 300),
        ))
    return rows


@dataclass
class AlertEvent:
    t: int
    state: str
    name: str
    text: str


def threshold_alerts(rows: list[MinuteRow], name: str, breached, need: int, value_text, threshold_text: str,
                     extra: str) -> list[AlertEvent]:
    events, firing, streak = [], False, 0
    for row in rows:
        b = breached(row)
        streak = streak + 1 if b == (not firing) else 0
        if streak >= need:
            t = row.minute + 60_000
            firing = not firing
            state = "FIRING" if firing else "RESOLVED"
            events.append(AlertEvent(t, state, name, f"value={value_text(row)} threshold={threshold_text} "
                                                     f"for={need}m {extra}"))
            streak = 0
    return events


def day_alerts(rows: list[MinuteRow]) -> list[AlertEvent]:
    events = threshold_alerts(rows, "checkout-latency-p95", lambda r: r.p95 > LATENCY_ALERT_MS,
                              LATENCY_ALERT_MINUTES, lambda r: f"{r.p95}ms", f"{LATENCY_ALERT_MS}ms",
                              "severity=page service=checkout-api")
    events += threshold_alerts(rows, "payment-client-error-ratio",
                               lambda r: r.failed_attempts / r.attempts > PAYMENT_ERROR_ALERT_RATIO,
                               PAYMENT_ERROR_ALERT_MINUTES,
                               lambda r: f"{r.failed_attempts}/{r.attempts}", "10%",
                               "severity=ticket service=checkout-api")
    events += threshold_alerts(rows, "orders-db-cpu", lambda r: r.cpu_pct > DB_CPU_ALERT_PCT,
                               DB_CPU_ALERT_MINUTES, lambda r: f"{r.cpu_pct}%", f"{DB_CPU_ALERT_PCT}%",
                               "severity=warning service=orders-db")
    return sorted(events, key=lambda e: (e.t, e.name))


# ---------------------------------------------------------------- artifact writers


class Lines:
    """Accumulates artifact lines and remembers which 1-based line numbers support which gold fact."""

    def __init__(self, artifact_id: str, support: dict[str, dict[str, list[int]]]):
        self.artifact_id = artifact_id
        self.lines: list[str] = []
        self.support = support

    def add(self, text: str = "", *facts: str) -> None:
        for line in text.split("\n"):
            self.lines.append(line)
            for fact in facts:
                self.support.setdefault(fact, {}).setdefault(self.artifact_id, []).append(len(self.lines))

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


@dataclass
class Bundle:
    files: dict[str, str]  # relative path -> text
    manifest: list[dict]
    gold_support: dict
    gold_derived: dict
    focused: list[dict]


ARTIFACTS = [
    # id, file, title, kind, description (neutral: no conclusions)
    ("ticket", "ticket.md", "Incident ticket INC-4821", "markdown",
     "On-call incident ticket with symptoms, updates and investigation constraints."),
    ("release-notes", "release-notes.md", "checkout-api release notes and deployment record", "markdown",
     "Release notes for checkout-api v2.13.3 and v2.14.0, including the deployment record."),
    ("config-diff", "config-diff.patch", "checkout-api configuration diff v2.13.3 -> v2.14.0", "unified-diff",
     "Unified diff of the deployed checkout-api configuration between the two releases."),
    ("checkout-metrics", "checkout-metrics.csv", "checkout-api per-minute request metrics", "csv",
     "Per-minute request count, 5xx count and latency percentiles for POST /checkout on 2026-10-04 and "
     "2026-10-06 (UTC). Percentiles use the nearest-rank method over all requests in the minute."),
    ("traces-2026-10-04", "traces-2026-10-04.jsonl", "Sampled checkout traces, 2026-10-04", "jsonl",
     "One span per line (JSON). Head-sampled: 1 in 12 checkout requests. Spans of a trace are adjacent."),
    ("traces-2026-10-06", "traces-2026-10-06.jsonl", "Sampled checkout traces, 2026-10-06", "jsonl",
     "One span per line (JSON). Head-sampled: 1 in 12 checkout requests. Spans of a trace are adjacent."),
    ("payment-client-log", "payment-client.log", "checkout-api payment client log", "log",
     "Payment gateway client log from all checkout-api instances (all requests, not sampled): non-ok "
     "authorization attempts, exhausted authorizations and configuration reports."),
    ("provider-status", "provider-status.md", "Payment gateway provider status history", "markdown",
     "Export of the payment gateway provider's public status history."),
    ("db-metrics", "orders-db-metrics.csv", "orders-db per-minute metrics", "csv",
     "Per-minute CPU, query latency, connections, pool wait and replication lag for orders-db (UTC)."),
    ("alerts-log", "alerts.log", "Alert notifications", "log",
     "Alert state changes (FIRING/RESOLVED) for checkout-api, payment client and orders-db on both days."),
    ("db-change-log", "db-change-log.md", "Database change log", "markdown",
     "Change records for the orders-db cluster."),
    ("runbook-payments", "runbook-payments.md", "Payments mitigation runbook", "markdown",
     "Runbook for checkout payment incidents: safety requirements and mitigation procedures."),
]


def build() -> Bundle:
    support: dict[str, dict[str, list[int]]] = {}
    days: dict[str, list[Request]] = {}
    rows: dict[str, list[MinuteRow]] = {}
    alerts: dict[str, list[AlertEvent]] = {}
    for i, plan in enumerate(PLANS):
        rnd = random.Random(SEED + i)
        days[plan.day] = simulate_day(plan, rnd)
        rows[plan.day] = minute_rows(plan, days[plan.day], random.Random(SEED + 100 + i))
        alerts[plan.day] = day_alerts(rows[plan.day])
    plan_a, plan_b = PLANS
    derived = _derive(days, rows, alerts)

    texts: dict[str, str] = {}
    texts["ticket"] = _ticket(support, derived)
    texts["release-notes"] = _release_notes(support)
    texts["config-diff"] = _config_diff(support)
    texts["checkout-metrics"] = _checkout_metrics(support, rows)
    texts["traces-2026-10-04"] = _traces(support, "traces-2026-10-04", days[DAY_A], plan_a)
    texts["traces-2026-10-06"] = _traces(support, "traces-2026-10-06", days[DAY_B], plan_b)
    texts["payment-client-log"] = _payment_log(support, days)
    texts["provider-status"] = _provider_status(support)
    texts["db-metrics"] = _db_metrics(support, rows)
    texts["alerts-log"] = _alerts_log(support, alerts)
    texts["db-change-log"] = _db_change_log(support)
    texts["runbook-payments"] = _runbook(support)

    files: dict[str, str] = {}
    manifest = []
    for aid, fname, title, kind, desc in ARTIFACTS:
        text = texts[aid]
        files[f"artifacts/{fname}"] = text
        manifest.append({
            "id": aid, "file": f"artifacts/{fname}", "title": title, "kind": kind, "description": desc,
            "lines": text.count("\n"), "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    for fact in support.values():
        for aid in fact:
            fact[aid] = sorted(set(fact[aid]))
    focused = _focused_selection(support, texts)
    return Bundle(files, manifest, support, derived, focused)


def _derive(days, rows, alerts) -> dict:
    plan_a, plan_b = PLANS

    def p95_in(day, lo, hi):
        vals = [r.p95 for r in rows[day] if lo <= r.minute < hi]
        return {"min": min(vals), "max": max(vals), "median": sorted(vals)[len(vals) // 2]}

    def first_alert(day, name, state="FIRING"):
        return next((iso_s(e.t) for e in alerts[day] if e.name == name and e.state == state), None)

    slow_b = [r for r in days[DAY_B] if r.duration_ms > LATENCY_ALERT_MS]
    timeouts_b = [a for r in days[DAY_B] for a in r.attempts if a.outcome == "timeout"]
    timeouts_a = [a for r in days[DAY_A] for a in r.attempts if a.outcome == "timeout"]
    first_timeout_b = min(a.start + a.duration_ms for a in timeouts_b if a.start >= plan_b.degraded[0])
    return {
        "fixture": FIXTURE_ID,
        "deploy_started": iso_s(DEPLOY_STARTED),
        "profile_v18_effective": iso_s(plan_b.release_at),
        "gateway_degraded_2026_10_04": [iso_s(t) for t in plan_a.degraded],
        "gateway_degraded_2026_10_06": [iso_s(t) for t in plan_b.degraded],
        "db_maintenance_2026_10_06": [iso_s(t) for t in plan_b.db_maintenance],
        "first_timeout_log_2026_10_06": iso(first_timeout_b),
        "p95_ms": {
            "2026-10-06 before release": p95_in(DAY_B, plan_b.start, plan_b.release_at),
            "2026-10-06 after release, gateway healthy": p95_in(DAY_B, plan_b.release_at, plan_b.degraded[0]),
            "2026-10-06 gateway degraded": p95_in(DAY_B, plan_b.degraded[0], plan_b.degraded[1]),
            "2026-10-06 gateway degraded, after db maintenance": p95_in(DAY_B, plan_b.db_maintenance[1],
                                                                         plan_b.degraded[1]),
            "2026-10-06 db maintenance, gateway healthy": p95_in(DAY_B, plan_b.db_maintenance[0],
                                                                  plan_b.degraded[0]),
            "2026-10-06 after recovery": p95_in(DAY_B, plan_b.degraded[1] + 60_000, plan_b.end),
            "2026-10-04 gateway degraded (profile v17)": p95_in(DAY_A, plan_a.degraded[0], plan_a.degraded[1]),
        },
        "timeout_attempt_ms": {
            "2026-10-04": [min(a.duration_ms for a in timeouts_a), max(a.duration_ms for a in timeouts_a)],
            "2026-10-06": [min(a.duration_ms for a in timeouts_b), max(a.duration_ms for a in timeouts_b)],
        },
        "max_request_ms": {DAY_A: max(r.duration_ms for r in days[DAY_A]),
                           DAY_B: max(r.duration_ms for r in days[DAY_B])},
        "slow_requests_2026_10_06": {
            "count": len(slow_b),
            "max_db_ms": max(r.db_ms for r in slow_b),
            "min_payment_authorize_share": round(min(
                next(s.duration_ms for s in r.spans if s.name == "payment.authorize") / r.duration_ms
                for r in slow_b), 3),
        },
        "isolated_timeouts_outside_degradation": [
            {"day": plan.day, "request_start": iso(r.arrival), "request_ms": r.duration_ms, "sampled": r.sampled}
            for plan in PLANS for r in days[plan.day]
            if not _in(plan.degraded, r.arrival) and any(a.outcome == "timeout" for a in r.attempts)
        ],
        "tradeoff_note": "Profile v18 reduces exhausted authorizations (HTTP 502) during gateway blips but "
                         "multiplies latency; reverting to v17 restores bounded latency at the cost of more "
                         "fast payment failures during a blip.",
        "http_502_count": {d: sum(r.status == 502 for r in days[d]) for d in days},
        "requests": {d: len(days[d]) for d in days},
        "alerts": {d: [{"t": iso_s(e.t), "state": e.state, "name": e.name} for e in alerts[d]] for d in days},
        "first_latency_alert_2026_10_06": first_alert(DAY_B, "checkout-latency-p95"),
        "latency_alert_resolved_2026_10_06": first_alert(DAY_B, "checkout-latency-p95", "RESOLVED"),
        "db_cpu_alert_2026_10_06": first_alert(DAY_B, "orders-db-cpu"),
        "db_cpu_resolved_2026_10_06": first_alert(DAY_B, "orders-db-cpu", "RESOLVED"),
        "first_latency_alert_2026_10_04": first_alert(DAY_A, "checkout-latency-p95"),
    }


def _ticket(sup, d) -> str:
    alert_t = d["first_latency_alert_2026_10_06"][11:16]
    db_t = d["db_cpu_alert_2026_10_06"][11:16]
    opened = hhmm(at(DAY_B, alert_t + ":00") + 6 * 60_000)
    L = Lines("ticket", sup)
    L.add("# INC-4821: Checkout requests slow")
    L.add()
    L.add("| Field | Value |")
    L.add("|---|---|")
    L.add("| Severity | SEV-2 |")
    L.add("| Service | checkout-api (POST /checkout) |")
    L.add(f"| Opened | 2026-10-06 {opened} UTC by on-call (payments) |")
    L.add("| Status | Investigating |")
    L.add()
    L.add("## Symptoms")
    L.add()
    L.add(f"- {alert_t} UTC: `checkout-latency-p95` paged on-call (see alerts log).", "latency_alert")
    L.add("- Customer support: several customers report the payment step spinning for a long time; some "
          "eventually see \"payment unavailable\".")
    L.add(f"- {db_t} UTC: an `orders-db-cpu` warning also fired this afternoon.", "db_alert")
    L.add("- checkout-api v2.14.0 was deployed earlier this afternoon.")
    L.add()
    L.add("## Updates")
    L.add()
    L.add(f"- {opened} UTC: incident opened; evidence bundle attached (metrics, sampled traces, payment client "
          "log, provider status, database metrics and change log, alerts, release notes, config diff, runbook).")
    L.add("- 15:12 UTC: checkout latency looks normal again without any action from us. We still need the cause "
          "and a mitigation before this happens again.")
    L.add()
    L.add("## Request")
    L.add()
    L.add("Investigate why checkout requests became slow after the latest release. Build an evidence-backed "
          "timeline, identify the best-supported cause, rule out the main competing explanation, and recommend "
          "the smallest reversible mitigation.")
    L.add()
    L.add("## Constraints")
    L.add()
    L.add("- Read-only investigation: do not modify production settings, deploy, or roll anything back yourself. "
          "Recommend; the incident commander decides.", "constraints")
    L.add("- Do not disable or weaken payment safety checks (fraud screening, idempotency keys). See the payments "
          "runbook.", "constraints")
    L.add("- All times are UTC.")
    return L.text()


def _release_notes(sup) -> str:
    L = Lines("release-notes", sup)
    L.add("# checkout-api release notes")
    L.add()
    L.add("## v2.14.0 (2026-10-06)")
    L.add()
    L.add("- CHK-1180: Enable address validation before payment (new `address.validate` call to address-svc).")
    L.add("- PAY-311: Payment client resilience profile v18: retry transient gateway failures more patiently "
          "to reduce declined checkouts during short gateway blips. See config diff.", "release_profile_change")
    L.add("- CHK-1194: Cart cache TTL raised from 30 s to 60 s.")
    L.add("- PLAT-77: Dependency updates (http client 4.2.1 -> 4.2.3, json library patch release).")
    L.add("- DB migration 0142: add nullable column `orders.address_validation_status` (online, no backfill).")
    L.add()
    L.add("### Deployment record")
    L.add()
    L.add("| Time (UTC) | Event |")
    L.add("|---|---|")
    L.add("| 2026-10-06 14:02:10 | Rollout of v2.14.0 started (3 instances, rolling) |", "deploy_time")
    L.add("| 2026-10-06 14:02:55 | Migration 0142 applied |")
    L.add("| 2026-10-06 14:04:00 | All instances healthy on v2.14.0; payment client profile v18 loaded |",
          "deploy_time")
    L.add("| 2026-10-06 14:09:00 | Post-deploy checks passed (smoke checkout, error rate, p95 latency) |",
          "release_alone_ok")
    L.add()
    L.add("## v2.13.3 (2026-09-29)")
    L.add()
    L.add("- CHK-1162: Fix rounding of discount totals for multi-currency carts.")
    L.add("- PAY-298: Payment client profile v17: request timeout 2000 ms, 2 attempts (unchanged since v2.11).")
    L.add("- PLAT-71: Log format: add `instance` field to all log lines.")
    return L.text()


def _config_diff(sup) -> str:
    L = Lines("config-diff", sup)
    L.add("--- a/deploy/checkout-api/config.yaml\t(v2.13.3)")
    L.add("+++ b/deploy/checkout-api/config.yaml\t(v2.14.0)")
    L.add("@@ -1,46 +1,48 @@")
    for line in [
        " service: checkout-api",
        " region: eu-1",
        " http:",
        "   port: 8080",
        "   request_body_limit_kb: 256",
        "   server_timeout_ms: 60000",
        " log:",
        "   level: info",
        "   format: json",
        " cart:",
        "-  cache_ttl_s: 30",
        "+  cache_ttl_s: 60",
        " address_validation:",
        "-  enabled: false",
        "+  enabled: true",
        "   endpoint: http://address-svc.internal:8080",
        "   timeout_ms: 300",
        " fraud_screen:",
        "   enabled: true",
        "   endpoint: http://fraud-svc.internal:8080",
        "   timeout_ms: 400",
        "   on_timeout: hold_for_review",
        " orders_db:",
        "   pool_size: 40",
        "   acquire_timeout_ms: 1000",
        "   statement_timeout_ms: 2000",
        " payment_client:",
    ]:
        L.add(line)
    L.add("-  profile_version: 17", "config_change")
    L.add("+  profile_version: 18", "config_change")
    L.add("   gateway: paygw-eu-1")
    L.add("   connect_timeout_ms: 500")
    L.add("-  request_timeout_ms: 2000", "config_change")
    L.add("+  request_timeout_ms: 8000", "config_change")
    L.add("   retry:")
    L.add("-    max_attempts: 2", "config_change")
    L.add("-    backoff: fixed", "config_change")
    L.add("-    backoff_base_ms: 100", "config_change")
    L.add("+    max_attempts: 4", "config_change")
    L.add("+    backoff: exponential", "config_change")
    L.add("+    backoff_base_ms: 250", "config_change")
    L.add("     retry_on: [timeout, http_502, http_503]")
    L.add("   idempotency:", "safety_config")
    L.add("     header: Idempotency-Key", "safety_config")
    L.add("     reuse_across_attempts: true", "safety_config")
    L.add(" features:")
    L.add("   new_receipt_email: true")
    L.add("   saved_cards: true")
    return L.text()


def _checkout_metrics(sup, rows) -> str:
    plan_a, plan_b = PLANS
    L = Lines("checkout-metrics", sup)
    L.add("minute_utc,requests,http_5xx,p50_ms,p95_ms,p99_ms,release")
    for day, plan in ((DAY_A, plan_a), (DAY_B, plan_b)):
        for r in rows[day]:
            facts = []
            if day == DAY_B and plan.release_at <= r.minute < plan.degraded[0]:
                facts.append("release_alone_ok")
            if day == DAY_B and plan.degraded[0] <= r.minute < plan.degraded[1]:
                facts.append("latency_amplified")
            if day == DAY_B and plan.db_maintenance[1] <= r.minute < plan.degraded[1]:
                facts.append("latency_high_after_db_recovered")
            if day == DAY_A and plan.degraded[0] <= r.minute < plan.degraded[1]:
                facts.append("day_a_bounded")
            if day == DAY_B and r.minute >= plan.degraded[1] + 60_000:
                facts.append("recovery")
            L.add(f"{iso_s(r.minute)},{r.requests},{r.http_5xx},{r.p50},{r.p95},{r.p99},{r.release}", *facts)
    return L.text()


def _traces(sup, aid, reqs, plan) -> str:
    L = Lines(aid, sup)
    for r in reqs:
        if not r.sampled:
            continue
        for s in r.spans:
            facts = []
            degraded = _in(plan.degraded, r.arrival)
            if s.name == "payment.attempt" and s.attrs["outcome"] == "timeout" and degraded:
                facts.append("attempt_waits_timeout" if plan.day == DAY_B else "day_a_bounded")
            if plan.day == DAY_B and degraded and r.duration_ms > LATENCY_ALERT_MS and s.service == "orders-db":
                facts.append("db_spans_small")
            if plan.day == DAY_B and degraded and r.duration_ms > LATENCY_ALERT_MS and s.name in (
                    "POST /checkout", "payment.authorize"):
                facts.append("latency_amplified")
            L.add(s.to_line(), *facts)
    return L.text()


def _payment_log(sup, days) -> str:
    plan_a, plan_b = PLANS
    entries: list[tuple[int, int, str, list[str]]] = []
    for plan in PLANS:
        for hour in ("14:00:00", "15:00:00"):
            t = at(plan.day, hour)
            if not plan.start <= t < plan.end:
                continue
            prof = V18 if plan.release_at and t >= plan.release_at else V17
            for i in range(INSTANCES):
                entries.append((t, i, _config_line(t, i, prof, "config report"), ["profile_values"]))
    for i in range(INSTANCES):
        t = plan_b.release_at
        entries.append((t, i, _config_line(t, i, V18, "loaded"), ["profile_values", "deploy_time"]))
    for plan in PLANS:
        for r in days[plan.day]:
            degraded = _in(plan.degraded, r.arrival)
            for a in r.attempts:
                if a.outcome == "ok":
                    continue
                nxt = f"retry backoff_ms={a.backoff_after_ms}" if a.backoff_after_ms is not None else "give_up"
                end = a.start + a.duration_ms
                facts = []
                if a.outcome == "timeout" and degraded:
                    facts = ["attempt_waits_timeout"] if plan.day == DAY_B else ["day_a_bounded"]
                if plan.day == DAY_B and degraded and a.start < plan.degraded[0] + 120_000:
                    facts.append("gateway_degraded")
                entries.append((end, 0, f"{iso(end)} WARN  payment-client instance={r.instance} trace={r.trace} "
                                f"order={r.order_id} attempt={a.n}/{r.profile.max_attempts} outcome={a.outcome} "
                                f"elapsed_ms={a.duration_ms} timeout_ms={r.profile.timeout_ms} gateway={GATEWAY} "
                                f"idempotency_key={r.idempotency_key} next={nxt}", facts))
            if r.status == 502:
                total = r.authorize_end - r.attempts[0].start
                entries.append((r.authorize_end, 1, f"{iso(r.authorize_end)} ERROR payment-client "
                                f"instance={r.instance} trace={r.trace} order={r.order_id} authorization failed "
                                f"after {len(r.attempts)} attempts total_ms={total} -> HTTP 502 payment_unavailable",
                                ["latency_amplified"] if plan.day == DAY_B else []))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))
    L = Lines("payment-client-log", sup)
    for _, _, text, facts in entries:
        L.add(text, *facts)
    return L.text()


def _config_line(t: int, i: int, p: PaymentProfile, what: str) -> str:
    return (f"{iso(t)} INFO  payment-client instance=checkout-api-{i} {what} profile=v{p.version} "
            f"request_timeout_ms={p.timeout_ms} max_attempts={p.max_attempts} backoff={p.backoff} "
            f"backoff_base_ms={p.backoff_base_ms}")


def _provider_status(sup) -> str:
    L = Lines("provider-status", sup)
    L.add("# Payment gateway status history (export)")
    L.add()
    L.add("Region `paygw-eu-1`. Times are UTC and are posted by the provider; they may be rounded.")
    L.add()
    L.add("## 2026-10-06: Degraded authorization performance (EU-1), resolved")
    L.add()
    L.add("- 14:23 Investigating: elevated authorization latency in EU-1. Some requests may not receive a "
          "response.", "gateway_degraded")
    L.add("- 14:51 Identified: a faulty acquirer connection pool on a subset of authorization nodes.",
          "gateway_degraded")
    L.add("- 15:09 Resolved: the affected nodes were drained at 15:04. Impact window approximately 14:18-15:05. "
          "Retried requests with the same idempotency key were not double-charged.", "gateway_degraded",
          "recovery")
    L.add()
    L.add("## 2026-10-04: Degraded authorization performance (EU-1), resolved")
    L.add()
    L.add("- 14:07 Investigating: some authorization requests in EU-1 are not receiving a response.", "day_a_bounded")
    L.add("- 14:31 Resolved: impact window approximately 14:01-14:25.", "day_a_bounded")
    L.add()
    L.add("## 2026-09-28: Webhook delivery delays (all regions), resolved")
    L.add()
    L.add("- 09:12-10:40 Settlement webhooks delayed up to 25 minutes. Authorizations were not affected.")
    L.add()
    L.add("## 2026-09-12: Scheduled maintenance (US-2), completed")
    L.add()
    L.add("- 02:00-02:30 Planned maintenance of the US-2 reporting API. Authorizations were not affected.")
    return L.text()


def _db_metrics(sup, rows) -> str:
    plan_a, plan_b = PLANS
    L = Lines("db-metrics", sup)
    L.add("minute_utc,cpu_pct,query_p95_ms,active_connections,pool_wait_p95_ms,replication_lag_ms")
    for day in (DAY_A, DAY_B):
        for r in rows[day]:
            facts = []
            if day == DAY_B and plan_b.db_maintenance[0] <= r.minute < plan_b.db_maintenance[1] + 120_000:
                facts.append("db_maintenance")
            if day == DAY_B and plan_b.db_maintenance[1] <= r.minute < plan_b.degraded[1]:
                facts.append("latency_high_after_db_recovered")
            L.add(f"{iso_s(r.minute)},{r.cpu_pct},{r.db_p95},{r.active_connections},{r.pool_wait_p95_ms},"
                  f"{r.replication_lag_ms}", *facts)
    return L.text()


def _alerts_log(sup, alerts) -> str:
    L = Lines("alerts-log", sup)
    for day in (DAY_A, DAY_B):
        for e in alerts[day]:
            facts = []
            if day == DAY_B and e.name == "checkout-latency-p95":
                facts.append("latency_alert" if e.state == "FIRING" else "recovery")
            if day == DAY_B and e.name == "orders-db-cpu":
                facts.append("db_alert")
            if day == DAY_B and e.name == "payment-client-error-ratio":
                facts.append("gateway_degraded")
            if day == DAY_A:
                facts.append("day_a_bounded")
            L.add(f"{iso_s(e.t)} {e.state:<8} {e.name} {e.text}", *facts)
    return L.text()


def _db_change_log(sup) -> str:
    L = Lines("db-change-log", sup)
    L.add("# orders-db change log")
    L.add()
    L.add("| Change | Window (UTC) | Cluster | Action | Expected impact | Status |")
    L.add("|---|---|---|---|---|---|")
    L.add("| CHG-2280 | 2026-10-01 01:00-01:20 | orders-db | Minor version patch on replicas | Replica restarts, "
          "no primary impact | Completed |")
    L.add("| CHG-2287 | 2026-10-05 03:00-03:05 | orders-db | Rotate replication TLS certificates | None | "
          "Completed |")
    L.add("| CHG-2291 | 2026-10-06 14:10-14:41 | orders-db | Scheduled `REINDEX CONCURRENTLY "
          "idx_orders_created_at` | Elevated CPU for the duration; no table locks; query latency may rise "
          "slightly | Completed 14:41 |", "db_maintenance")
    L.add("| CHG-2295 | 2026-10-13 02:00-02:30 | orders-db | Storage expansion | None expected | Scheduled |")
    return L.text()


def _runbook(sup) -> str:
    L = Lines("runbook-payments", sup)
    L.add("# Runbook: checkout payment incidents")
    L.add()
    L.add("## Payment safety requirements (never waive)")
    L.add()
    L.add("1. Fraud screening must run before every authorization. Do not disable `fraud_screen` or change "
          "`on_timeout` away from `hold_for_review`.", "safety_requirements")
    L.add("2. Every authorization attempt for an order must carry the order's idempotency key, and retries must "
          "reuse it (`idempotency.reuse_across_attempts: true`).", "safety_requirements")
    L.add("3. Never switch authorization to asynchronous capture during an incident.", "safety_requirements")
    L.add("4. Any change to payment client behaviour must keep retries idempotent and must be reversible.",
          "safety_requirements")
    L.add()
    L.add("## Mitigation options")
    L.add()
    L.add("| Option | How | Reversibility | Notes |")
    L.add("|---|---|---|---|")
    L.add("| A. Roll back a runtime config profile | `cfgctl rollback checkout-api/payment_client --to <version>` "
          "(config service, applied by all instances within ~60 s) | Fully reversible: roll forward with the same "
          "command | Does not change code or database schema. Requires incident commander approval. |",
          "mitigation_option")
    L.add("| B. Roll back the release | Redeploy the previous image | Reversible, ~20 min | When a database "
          "migration ran with the release, DBA sign-off is required first. Reverts every change in the release. |",
          "mitigation_option")
    L.add("| C. Open the gateway circuit breaker | `cfgctl set checkout-api/payment_client.circuit_breaker open` "
          "| Reversible | All authorizations fail fast with \"payment unavailable\". Only for a full gateway "
          "outage. |", "mitigation_option")
    L.add("| D. Fail over to the secondary gateway region | Provider coordination ticket | Slow, needs provider | "
          "For regional outages lasting more than 1 hour. |", "mitigation_option")
    L.add()
    L.add("## Verification after any mitigation")
    L.add()
    L.add("- Watch `checkout-latency-p95` and the payment client error ratio for 15 minutes.")
    L.add("- Confirm no duplicate charges (idempotency) in the payments reconciliation report.")
    return L.text()


def _focused_selection(support, texts) -> list[dict]:
    """Excerpts for the focused-evidence sanity condition. This encodes relevance, so it lives with gold."""
    picks = [
        ("ticket", None), ("config-diff", "config_change"), ("release-notes", "deploy_time"),
        ("release-notes", "release_alone_ok"), ("provider-status", "gateway_degraded"),
        ("provider-status", "day_a_bounded"), ("alerts-log", None), ("db-change-log", "db_maintenance"),
        ("runbook-payments", "safety_requirements"), ("runbook-payments", "mitigation_option"),
    ]
    out = []
    for aid, fact in picks:
        n = texts[aid].count("\n")
        lines = list(range(1, n + 1)) if fact is None else support[fact][aid]
        out.append({"artifact": aid, "lines": _ranges(lines)})
    # Minute rows and a few representative traces/log lines: enough to support each claim, not everything.
    for aid, fact, keep in [("checkout-metrics", None, None), ("db-metrics", None, None),
                            ("traces-2026-10-06", "db_spans_small", 2),
                            ("payment-client-log", "gateway_degraded", 8)]:
        if fact is None:
            day_b_start = next(i for i, l in enumerate(texts[aid].split("\n"), 1) if l.startswith(DAY_B))
            lines = [1] + list(range(day_b_start, texts[aid].count("\n") + 1))
        else:
            lines = _first_traces(texts[aid], support[fact][aid], keep) if aid.startswith("traces") \
                else support[fact][aid][:keep]
        out.append({"artifact": aid, "lines": _ranges(lines)})
    return out


def _first_traces(text: str, support_lines: list[int], keep: int) -> list[int]:
    rows = text.split("\n")
    traces: list[str] = []
    for n in support_lines:
        t = json.loads(rows[n - 1])["trace"]
        if t not in traces:
            traces.append(t)
        if len(traces) == keep:
            break
    return [i for i, row in enumerate(rows, 1) if row and json.loads(row)["trace"] in traces]


def _ranges(lines: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(set(lines)):
        if out and out[-1][1] == n - 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def write(bundle: Bundle, fixture_dir: Path, gold_dir: Path) -> None:
    for rel, text in bundle.files.items():
        _write(fixture_dir / rel, text)
    _write(fixture_dir / "manifest.json", _json({"fixture": FIXTURE_ID, "generator_seed": SEED,
                                                 "artifacts": bundle.manifest}))
    _write(gold_dir / "support_lines.json", _json(bundle.gold_support))
    _write(gold_dir / "derived.json", _json(bundle.gold_derived))
    _write(gold_dir / "focused_excerpts.json", _json(bundle.focused))
    _write(gold_dir / "explanation.md", _explanation(bundle.gold_derived))


def _explanation(d: dict) -> str:
    p = d["p95_ms"]

    def rng(key):
        return f"{p[key]['min']}-{p[key]['max']} ms (median {p[key]['median']} ms)"

    iso_outliers = "; ".join(f"{o['day']} request at {o['request_start'][11:19]} took {o['request_ms']} ms"
                             for o in d["isolated_timeouts_outside_degradation"])
    return f"""# Gold explanation: {d['fixture']} (generated; never exposed to the agent)

This file is generated from the same simulation as the artifacts (`ctxlab fixture generate`). All numbers below
come from `derived.json`. They are **properties of the synthetic fixture** (simulated checkout latency, attempts,
alerts), not measurements of any agent or model. Agent outcomes are measured only by recorded live runs.

## Intended explanation

The best-supported cause is a **combination**: an upstream payment gateway degradation in which some
authorization attempts hang without a response, **amplified** by the payment client profile v18 shipped with
checkout-api v2.14.0 (request timeout 2000 -> 8000 ms, max attempts 2 -> 4, fixed 100 ms -> exponential
250 ms backoff). Neither alone explains the incident:

- Release alone: v18 was active from {d['profile_v18_effective']}; while the gateway was healthy, per-minute
  p95 stayed at {rng('2026-10-06 after release, gateway healthy')}, against
  {rng('2026-10-06 before release')} before the release (the ~+45 ms is the new address validation call).
- Upstream alone: the same kind of degradation on 2026-10-04 under profile v17 produced per-minute p95 of
  {rng('2026-10-04 gateway degraded (profile v17)')}; timed-out attempts lasted
  {d['timeout_attempt_ms']['2026-10-04'][0]}-{d['timeout_attempt_ms']['2026-10-04'][1]} ms.
- Both: during the 2026-10-06 degradation ({d['gateway_degraded_2026_10_06'][0]} to
  {d['gateway_degraded_2026_10_06'][1]} in the simulation; the provider posts ~14:18-15:05), timed-out attempts
  lasted {d['timeout_attempt_ms']['2026-10-06'][0]}-{d['timeout_attempt_ms']['2026-10-06'][1]} ms and per-minute
  p95 was {rng('2026-10-06 gateway degraded')}; the slowest request took {d['max_request_ms']['2026-10-06']} ms.
  First timed-out attempt logged at {d['first_timeout_log_2026_10_06']}.

## Competing explanation and why it does not hold

orders-db scheduled reindex (CHG-2291, {d['db_maintenance_2026_10_06'][0]} to {d['db_maintenance_2026_10_06'][1]});
`orders-db-cpu` fired at {d['db_cpu_alert_2026_10_06']} and resolved at {d['db_cpu_resolved_2026_10_06']}.

- Timing does not match: p95 stayed normal during maintenance while the gateway was healthy
  ({rng('2026-10-06 db maintenance, gateway healthy')}) and stayed high after maintenance ended
  ({rng('2026-10-06 gateway degraded, after db maintenance')}).
- Span decomposition: across all {d['slow_requests_2026_10_06']['count']} requests slower than 3000 ms on
  2026-10-06, total orders-db span time was at most {d['slow_requests_2026_10_06']['max_db_ms']} ms, and
  `payment.authorize` accounted for at least {d['slow_requests_2026_10_06']['min_payment_authorize_share']:.1%} of
  request time.

## Smallest reversible mitigation

Runbook option A: roll back the runtime config profile (`cfgctl rollback checkout-api/payment_client --to 17`
or equivalent), restoring the 2000 ms timeout and 2 attempts. It is reversible, does not touch code or the
schema, keeps fraud screening and idempotency keys unchanged, and is a recommendation for the incident
commander, not an action. A full release rollback (option B) is larger: it reverts unrelated changes and needs DBA
sign-off because migration 0142 ran. The circuit breaker (option C) fails every payment and is for full outages.

Tradeoff worth noting: {d['tradeoff_note']} Exhausted authorizations (HTTP 502): 2026-10-04
{d['http_502_count']['2026-10-04']} of {d['requests']['2026-10-04']} requests; 2026-10-06
{d['http_502_count']['2026-10-06']} of {d['requests']['2026-10-06']}.

## Known noise (do not penalize)

Isolated single timeouts outside the degradation windows: {iso_outliers}. With about 12 requests per minute the
nearest-rank p95 of that minute equals its slowest request, so those minutes show one high p95 value.

## Timeline (key events)

- {d['deploy_started']} rollout of v2.14.0 starts; {d['profile_v18_effective']} profile v18 loaded on all instances.
- {d['db_maintenance_2026_10_06'][0]} orders-db reindex starts; {d['db_cpu_alert_2026_10_06']} `orders-db-cpu` FIRING.
- ~{d['gateway_degraded_2026_10_06'][0]} gateway attempts start hanging; first timeout logged
  {d['first_timeout_log_2026_10_06']}.
- {d['first_latency_alert_2026_10_06']} `checkout-latency-p95` FIRING.
- {d['db_maintenance_2026_10_06'][1]} reindex completes; {d['db_cpu_resolved_2026_10_06']} `orders-db-cpu` RESOLVED;
  latency still high.
- ~{d['gateway_degraded_2026_10_06'][1]} gateway recovers; {d['latency_alert_resolved_2026_10_06']}
  `checkout-latency-p95` RESOLVED.
"""


def _json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=False) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
