# Gold explanation: checkout-latency-v1 (generated; never exposed to the agent)

This file is generated from the same simulation as the artifacts (`ctxlab fixture generate`). All numbers below
come from `derived.json`. They are **properties of the synthetic fixture** (simulated checkout latency, attempts,
alerts), not measurements of any agent or model. Agent outcomes are measured only by recorded live runs.

## Intended explanation

The best-supported cause is a **combination**: an upstream payment gateway degradation in which some
authorization attempts hang without a response, **amplified** by the payment client profile v18 shipped with
checkout-api v2.14.0 (request timeout 2000 -> 8000 ms, max attempts 2 -> 4, fixed 100 ms -> exponential
250 ms backoff). Neither alone explains the incident:

- Release alone: v18 was active from 2026-10-06T14:04:00Z; while the gateway was healthy, per-minute
  p95 stayed at 465-564 ms (median 529 ms), against
  386-509 ms (median 484 ms) before the release (the ~+45 ms is the new address validation call).
- Upstream alone: the same kind of degradation on 2026-10-04 under profile v17 produced per-minute p95 of
  2500-4201 ms (median 4183 ms); timed-out attempts lasted
  2001-2006 ms.
- Both: during the 2026-10-06 degradation (2026-10-06T14:19:00Z to
  2026-10-06T15:04:00Z in the simulation; the provider posts ~14:18-15:05), timed-out attempts
  lasted 8001-8006 ms and per-minute
  p95 was 8545-33908 ms (median 17162 ms); the slowest request took 33908 ms.
  First timed-out attempt logged at 2026-10-06T14:19:19.946Z.

## Competing explanation and why it does not hold

orders-db scheduled reindex (CHG-2291, 2026-10-06T14:10:00Z to 2026-10-06T14:41:00Z);
`orders-db-cpu` fired at 2026-10-06T14:15:00Z and resolved at 2026-10-06T14:46:00Z.

- Timing does not match: p95 stayed normal during maintenance while the gateway was healthy
  (472-564 ms (median 522 ms)) and stayed high after maintenance ended
  (8545-33882 ms (median 17162 ms)).
- Span decomposition: across all 195 requests slower than 3000 ms on
  2026-10-06, total orders-db span time was at most 54 ms, and
  `payment.authorize` accounted for at least 98.2% of
  request time.

## Smallest reversible mitigation

Runbook option A: roll back the runtime config profile (`cfgctl rollback checkout-api/payment_client --to 17`
or equivalent), restoring the 2000 ms timeout and 2 attempts. It is reversible, does not touch code or the
schema, keeps fraud screening and idempotency keys unchanged, and is a recommendation for the incident
commander, not an action. A full release rollback (option B) is larger: it reverts unrelated changes and needs DBA
sign-off because migration 0142 ran. The circuit breaker (option C) fails every payment and is for full outages.

Tradeoff worth noting: Profile v18 reduces exhausted authorizations (HTTP 502) during gateway blips but multiplies latency; reverting to v17 restores bounded latency at the cost of more fast payment failures during a blip. Exhausted authorizations (HTTP 502): 2026-10-04
37 of 1080 requests; 2026-10-06
8 of 1620.

## Known noise (do not penalize)

Isolated single timeouts outside the degradation windows: 2026-10-04 request at 14:47:26 took 2573 ms; 2026-10-06 request at 15:22:26 took 8756 ms; 2026-10-06 request at 15:34:56 took 8732 ms. With about 12 requests per minute the
nearest-rank p95 of that minute equals its slowest request, so those minutes show one high p95 value.

## Timeline (key events)

- 2026-10-06T14:02:10Z rollout of v2.14.0 starts; 2026-10-06T14:04:00Z profile v18 loaded on all instances.
- 2026-10-06T14:10:00Z orders-db reindex starts; 2026-10-06T14:15:00Z `orders-db-cpu` FIRING.
- ~2026-10-06T14:19:00Z gateway attempts start hanging; first timeout logged
  2026-10-06T14:19:19.946Z.
- 2026-10-06T14:22:00Z `checkout-latency-p95` FIRING.
- 2026-10-06T14:41:00Z reindex completes; 2026-10-06T14:46:00Z `orders-db-cpu` RESOLVED;
  latency still high.
- ~2026-10-06T15:04:00Z gateway recovers; 2026-10-06T15:07:00Z
  `checkout-latency-p95` RESOLVED.
