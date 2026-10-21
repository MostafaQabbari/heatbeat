# Measurements

All numbers below were measured, not estimated. Data is **simulated** (seeded gas
boilers). Environment: one Linux cloud VM, PostgreSQL 16 + TimescaleDB 2.17.2,
Mosquitto 2.0.18, bridge with batch size 200. Your laptop will give different
numbers; re-measure before you rely on them.

## Ingest throughput (backlog upload, scenario S08)
50 plants upload a 6-hour backlog at once (like gateways after an outage):
180,000 rows in 7,200 messages.

| Run | Time until all rows stored | Rows per second |
|---|---|---|
| 1 | 6.8 s | about 26,400 |
| 2 | 6.2 s | about 29,000 |

Design target: 4,000 rows/s, so one bridge has about 6x headroom.

Same backlog again, but into **already-compressed** chunks: about 30 s (about 5x slower).

## Storage

| | Size for 180,000 rows | Bytes per row |
|---|---|---|
| Uncompressed (table + indexes) | 19 MB | about 112 |
| Compressed | 1.3 MB | about 7 |

Compression ratio: about 16x (`chunk_compression_stats`).

At the design target (345 million rows/day) that would be about 39 GB/day
uncompressed and about 2.5 GB/day compressed.

## Database outage during ingest (scenario S02)
PostgreSQL stopped while a 30-minute backlog for 4 plants arrived. The bridge logged
4 failed batch writes and acknowledged nothing. After the restart all 6,000 rows
were present, with 6,000 distinct keys (no duplicates).

## Larger backlog (7 days, 20 plants, mixed plant types with faults)
`heatbeat-simulator --plants 20 --backfill-hours 168 --with-faults --once`: about 1.9 million rows,
all stored in under 90 s (polled every 10 s, so this is an upper bound), consistent with the
throughput above. This data set is the one used for `docs/query-plans.md`.

## Failures while data flows (scenario S22)
Bridge crash, database outage and broker restart, each for 20 s under load: 0 lost and
0 duplicated readings, all stored within about 2 s of the restart. See `docs/chaos-report.md`
and ADR 0010 for what the first run found (145 of 200 readings lost) and why.

## API query latency
On the 2-million-row data set, through the tenant-filtering views (`docs/query-plans.md`):
latest values of a plant about 60-90 ms, a 6-hour raw series about 40-50 ms, a 7-day
hourly series about 35-45 ms. Single runs on a VM, not percentiles under load.

## Limits found on the way
- TimescaleDB 2.17 refuses row-level security on a compressed hypertable (ADR 0005).
- Changing many rows inside compressed chunks hits `timescaledb.max_tuples_decompressed_per_dml_transaction`
  (100,000 by default). Migration 0003's downgrade turns the limit off (0) for its own transaction only.

## How to reproduce
```
uv run heatbeat-bridge
uv run heatbeat-simulator --plants 50 --backfill-hours 6 --once
```
Count rows in `measurement` until the expected number is reached.
