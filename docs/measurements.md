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

## How to reproduce
```
uv run heatbeat-bridge
uv run heatbeat-simulator --plants 50 --backfill-hours 6 --once
```
Count rows in `measurement` until the expected number is reached.
