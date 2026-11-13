# checkout-api release notes

## v2.14.0 (2026-10-06)

- CHK-1180: Enable address validation before payment (new `address.validate` call to address-svc).
- PAY-311: Payment client resilience profile v18: retry transient gateway failures more patiently to reduce declined checkouts during short gateway blips. See config diff.
- CHK-1194: Cart cache TTL raised from 30 s to 60 s.
- PLAT-77: Dependency updates (http client 4.2.1 -> 4.2.3, json library patch release).
- DB migration 0142: add nullable column `orders.address_validation_status` (online, no backfill).

### Deployment record

| Time (UTC) | Event |
|---|---|
| 2026-10-06 14:02:10 | Rollout of v2.14.0 started (3 instances, rolling) |
| 2026-10-06 14:02:55 | Migration 0142 applied |
| 2026-10-06 14:04:00 | All instances healthy on v2.14.0; payment client profile v18 loaded |
| 2026-10-06 14:09:00 | Post-deploy checks passed (smoke checkout, error rate, p95 latency) |

## v2.13.3 (2026-09-29)

- CHK-1162: Fix rounding of discount totals for multi-currency carts.
- PAY-298: Payment client profile v17: request timeout 2000 ms, 2 attempts (unchanged since v2.11).
- PLAT-71: Log format: add `instance` field to all log lines.
