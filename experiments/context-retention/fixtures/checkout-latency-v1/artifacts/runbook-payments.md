# Runbook: checkout payment incidents

## Payment safety requirements (never waive)

1. Fraud screening must run before every authorization. Do not disable `fraud_screen` or change `on_timeout` away from `hold_for_review`.
2. Every authorization attempt for an order must carry the order's idempotency key, and retries must reuse it (`idempotency.reuse_across_attempts: true`).
3. Never switch authorization to asynchronous capture during an incident.
4. Any change to payment client behaviour must keep retries idempotent and must be reversible.

## Mitigation options

| Option | How | Reversibility | Notes |
|---|---|---|---|
| A. Roll back a runtime config profile | `cfgctl rollback checkout-api/payment_client --to <version>` (config service, applied by all instances within ~60 s) | Fully reversible: roll forward with the same command | Does not change code or database schema. Requires incident commander approval. |
| B. Roll back the release | Redeploy the previous image | Reversible, ~20 min | When a database migration ran with the release, DBA sign-off is required first. Reverts every change in the release. |
| C. Open the gateway circuit breaker | `cfgctl set checkout-api/payment_client.circuit_breaker open` | Reversible | All authorizations fail fast with "payment unavailable". Only for a full gateway outage. |
| D. Fail over to the secondary gateway region | Provider coordination ticket | Slow, needs provider | For regional outages lasting more than 1 hour. |

## Verification after any mitigation

- Watch `checkout-latency-p95` and the payment client error ratio for 15 minutes.
- Confirm no duplicate charges (idempotency) in the payments reconciliation report.
