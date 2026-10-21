# ADR 0003: No Kafka (for now), and what would change my mind

Status: accepted (2026-10-21)

## Context
A log like Kafka between broker and database would add durable buffering, replay,
and many independent consumers. It also adds a cluster to operate.

## Decision
No Kafka. Gateway → Mosquitto → bridge → TimescaleDB.

- Buffering during outages: the gateway (store and forward) and the broker's
  persistent session (`max_queued_messages 100000`).
- Replay: from the database, which stores every reading.
- Throughput: one bridge stored about 26,000 to 29,000 rows/s in my measurement
  (docs/measurements.md). The design target is 4,000 rows/s.

The bridge writes through a small `Sink` interface, so a Kafka-based sink could be
added later without touching the rest.

## What would change my mind
- More than one team needs the raw stream.
- Replay beyond the database retention is required.
- Outages longer than gateways and broker can buffer.
- The single bridge becomes the bottleneck in the load report.

Book: Software Architecture: The Hard Parts, ch. 1: there is no best practice,
only the least-bad trade-off for this context; record it in an ADR (p. 5).
