## Timeline

- **14:02–14:09:** The v2.14.0 rollout began at 14:02:10, migration 0142 was applied at 14:02:55, and all instances were healthy with payment profile v18 loaded by 14:04. Post-deploy checks passed at 14:09. [release-notes:L15-L18]
- **14:10 onward:** The scheduled orders-DB reindex ran until 14:41; its change record anticipated elevated CPU but no table locks and only a possible slight query-latency rise. [db-change-log:L7]
- **Approximately 14:18:** The payment provider’s reported EU-1 impact began. Checkout metrics were near normal at 14:18, then showed elevated latency from 14:19 onward; the checkout p95 alert fired at 14:22. [provider-status:L7-L9] [checkout-metrics:L140-L145] [alerts-log:L7]
- **14:22–14:24:** Sampled traces show the long part of slow checkouts was payment authorization: one took 8,562 ms, including an 8,005 ms timeout followed by a successful retry; another took 8,543 ms with the same timeout-then-success pattern. By comparison, cart load and order persistence took 16–24 ms, address validation 38–39 ms, and fraud screening 34–43 ms. A nearby checkout at 14:23 completed in 412 ms, including 288 ms for payment. [traces-2026-10-06:L331-L353]
- **14:51–15:09:** The provider identified a faulty acquirer connection pool at 14:51, drained affected nodes at 15:04, and reported the impact resolved at 15:09. Checkout metrics were back near normal from 15:04, and the checkout latency alert resolved at 15:07. The ticket records normal latency by 15:12 without intervention by the team. [provider-status:L8-L9] [checkout-metrics:L186-L194] [alerts-log:L10] [ticket:L19-L20]

## Best-supported cause

The best-supported explanation is **EU-1 authorization degradation, amplified by v2.14.0’s more patient payment retry profile**. The provider reported that some authorizations were not receiving a response during the incident window, and sampled slow traces directly show an 8-second payment timeout consuming nearly all of the checkout delay. [provider-status:L7-L9] [traces-2026-10-06:L335-L338]

Profile v18 raised the request timeout from 2,000 ms to 8,000 ms, increased the maximum attempts from two to four, and changed backoff from fixed to exponential. The traces show the 8-second timeout followed by a successful retry, consistent with those settings prolonging affected requests. Idempotency-key reuse remained enabled. [config-diff:L30-L47] [traces-2026-10-06:L336-L337]

Provider degradation was not unique to this release: on Oct 4, checkout p95 rose to about 4.2 seconds while v2.13.3/v17 was running, during a separately reported EU-1 provider incident. This supports the provider as an independent trigger; the evidence supports v18 as an amplifier, not the sole cause. [provider-status:L11-L14] [checkout-metrics:L32-L58]

## Competing explanation

**Orders-DB CPU/reindex is the main competing explanation, but the evidence does not support it as the primary cause of the multi-second checkout delays.** The reindex and high CPU overlapped the incident: CPU reached 80–92% during the reindex window, and the CPU alert fired at 14:15. [db-change-log:L7] [db-metrics:L132-L163] [alerts-log:L5]

However, during that window query p95 was 14–30 ms and pool-wait p95 was 1–3 ms; in the sampled slow trace, cart load and order persistence were only 24 ms and 30 ms, versus 8,562 ms in payment authorization. [db-metrics:L132-L163] [traces-2026-10-06:L332-L338] The Oct 4 provider-related latency spike also occurred with DB CPU at 26–38%, query p95 at 14–18 ms, and pool wait at 0–1 ms. [checkout-metrics:L32-L58] [db-metrics:L32-L58] The reindex could have contributed minor delay, but it does not explain the dominant wait shown in the traces.

## Recommended mitigation

**If the problem recurs and persists, recommend an incident-commander-approved rollback of only the payment-client runtime profile to v17**, rather than rolling back the full release. The runbook describes this as reversible and applicable within about 60 seconds. [runbook-payments:L10-L15] It would reduce the prolonged timeout/retry behavior while leaving code and the database migration untouched; weigh the faster failure behavior against the risk of more payment failures. [release-notes:L23-L24] [config-diff:L35-L44]

Keep fraud screening and idempotency protections unchanged. After any approved profile change, monitor checkout p95 and payment-client error ratio for 15 minutes and check reconciliation for duplicate charges. [runbook-payments:L3-L8] [runbook-payments:L19-L22] No production change was made during this read-only investigation. [ticket:L26-L29]

## Uncertainty

The provider reports rounded times, and the traces are samples rather than a complete accounting of all checkouts. [provider-status:L3] The evidence strongly localizes sampled slow requests to payment, but does not quantify the share of all incident requests attributable to provider degradation versus any smaller contribution from DB activity or other factors. The incident had resolved in the available evidence, so the profile rollback is a contingency recommendation, not an assertion that it should be applied now. [ticket:L19-L20]
