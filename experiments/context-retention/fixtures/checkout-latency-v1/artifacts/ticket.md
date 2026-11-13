# INC-4821: Checkout requests slow

| Field | Value |
|---|---|
| Severity | SEV-2 |
| Service | checkout-api (POST /checkout) |
| Opened | 2026-10-06 14:28 UTC by on-call (payments) |
| Status | Investigating |

## Symptoms

- 14:22 UTC: `checkout-latency-p95` paged on-call (see alerts log).
- Customer support: several customers report the payment step spinning for a long time; some eventually see "payment unavailable".
- 14:15 UTC: an `orders-db-cpu` warning also fired this afternoon.
- checkout-api v2.14.0 was deployed earlier this afternoon.

## Updates

- 14:28 UTC: incident opened; evidence bundle attached (metrics, sampled traces, payment client log, provider status, database metrics and change log, alerts, release notes, config diff, runbook).
- 15:12 UTC: checkout latency looks normal again without any action from us. We still need the cause and a mitigation before this happens again.

## Request

Investigate why checkout requests became slow after the latest release. Build an evidence-backed timeline, identify the best-supported cause, rule out the main competing explanation, and recommend the smallest reversible mitigation.

## Constraints

- Read-only investigation: do not modify production settings, deploy, or roll anything back yourself. Recommend; the incident commander decides.
- Do not disable or weaken payment safety checks (fraud screening, idempotency keys). See the payments runbook.
- All times are UTC.
