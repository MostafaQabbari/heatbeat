# ADR 0002: At-least-once delivery + idempotent writes + acknowledge after commit

Status: accepted (2026-10-21)

## Context
Gateways publish with MQTT QoS 1, which is *at-least-once*: a message can arrive
twice (for example, after a reconnect). The database or the bridge can fail at any
moment. We must not lose readings, and we must not store them twice.

## Decision
1. The bridge uses `manual_ack`: a message is acknowledged to the broker **only
   after** the batch that contains it has been committed in PostgreSQL.
2. Each batch is written in **one transaction** (all or nothing).
3. Writes are **idempotent**: `UNIQUE (plant_id, datapoint_id, ts)` + `ON CONFLICT DO NOTHING`.
4. The bridge uses a **persistent session** (`clean_start=False`, session expiry 24 h),
   so the broker queues messages while the bridge is down.
5. Bad messages are not dropped silently: they go to `rejected_message` with a
   reason, and are then acknowledged (a "poison" message must not block the queue).

## Consequences
- Crash before commit: the broker redelivers, nothing is lost (test S02).
- Crash after commit, before ack: redelivery, the unique key ignores it (test S01).
- Database down: nothing is acknowledged, so the broker stops sending after
  `max_inflight_messages` (1000). This is natural **backpressure**; the bridge
  retries with exponential backoff and jitter.
- The result is "effectively once" storage, not exactly-once delivery.

Books:
- DDIA 2nd ed., ch. 12 "Stream Processing", Idempotence (p. 528).
- System Design Interview Vol. 1, ch. 10: exactly-once delivery is not possible,
  so dedupe by event id.
