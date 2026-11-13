# Payment gateway status history (export)

Region `paygw-eu-1`. Times are UTC and are posted by the provider; they may be rounded.

## 2026-10-06: Degraded authorization performance (EU-1), resolved

- 14:23 Investigating: elevated authorization latency in EU-1. Some requests may not receive a response.
- 14:51 Identified: a faulty acquirer connection pool on a subset of authorization nodes.
- 15:09 Resolved: the affected nodes were drained at 15:04. Impact window approximately 14:18-15:05. Retried requests with the same idempotency key were not double-charged.

## 2026-10-04: Degraded authorization performance (EU-1), resolved

- 14:07 Investigating: some authorization requests in EU-1 are not receiving a response.
- 14:31 Resolved: impact window approximately 14:01-14:25.

## 2026-09-28: Webhook delivery delays (all regions), resolved

- 09:12-10:40 Settlement webhooks delayed up to 25 minutes. Authorizations were not affected.

## 2026-09-12: Scheduled maintenance (US-2), completed

- 02:00-02:30 Planned maintenance of the US-2 reporting API. Authorizations were not affected.
