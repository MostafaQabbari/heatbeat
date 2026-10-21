# ADR 0004: Synchronous Python (threads) instead of asyncio for the bridge

Status: accepted (2026-10-21)

## Context
The bridge waits on two kinds of I/O: the MQTT broker and the database. asyncio
is the usual choice for that. But on Windows (the development machine) both
common async MQTT clients and async psycopg need a special event loop, and
manual acknowledgement is simplest in the synchronous `paho-mqtt` client.

## Decision
- `paho-mqtt` runs its network loop in a background thread and puts messages in a
  `queue.Queue`.
- The main thread batches, writes with synchronous `psycopg`, then acknowledges.
- The batching logic (`Ingestor`) knows nothing about MQTT or threads, so it is
  tested with fakes and a fake clock.
- FastAPI endpoints are plain `def` functions; FastAPI runs them in a thread pool.

## Consequences
- Same behaviour on Windows, Linux and in Docker; simple to read and debug.
- One bridge process handles about 26,000 to 29,000 rows/s (measured), above the target.
- To scale out: several bridges with an MQTT shared subscription (`$share/ingest/...`).
  Moving to asyncio would only be worth it if one process must hold many more connections.
