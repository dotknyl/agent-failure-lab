# orders-db change log

| Change | Window (UTC) | Cluster | Action | Expected impact | Status |
|---|---|---|---|---|---|
| CHG-2280 | 2026-10-01 01:00-01:20 | orders-db | Minor version patch on replicas | Replica restarts, no primary impact | Completed |
| CHG-2287 | 2026-10-05 03:00-03:05 | orders-db | Rotate replication TLS certificates | None | Completed |
| CHG-2291 | 2026-10-06 14:10-14:41 | orders-db | Scheduled `REINDEX CONCURRENTLY idx_orders_created_at` | Elevated CPU for the duration; no table locks; query latency may rise slightly | Completed 14:41 |
| CHG-2295 | 2026-10-13 02:00-02:30 | orders-db | Storage expansion | None expected | Scheduled |
