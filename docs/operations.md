# Operations: SLOs, alerts and runbooks

What this file does: says what "working" means in numbers (SLOs), how it is
measured, and what to do when an alert fires. Every alert in
`monitoring/alerts.yml` links to a section here.

## Service level objectives

| SLO | Target | Measured by |
|---|---|---|
| Data freshness | 99 % of readings are queryable within 30 s of being measured | `heatbeat_ingest_lag_seconds` (histogram, device time -> commit) |
| Ingest correctness | 0 lost and 0 duplicated readings under single-component failures | chaos test, `docs/chaos-report.md` |
| Alarm notification | A firing alarm is notified within 2 min | alarm engine every 60 s with 60 s watermark + notifier every 10 s; `heatbeat_notifications_pending` |
| API availability | 99 % of requests answer without a 5xx | `heatbeat_http_requests_total{status=~"5.."}` |
| API latency | p95 below 500 ms | `heatbeat_http_request_seconds` |

These are targets for this project, not measured over months of production.

## Where to look

| What | Where |
|---|---|
| Dashboard | Grafana http://127.0.0.1:3300 -> "Heatbeat overview" (profile `monitoring`) |
| Raw metrics | API `/metrics`, bridge `:9101/metrics`, Prometheus http://127.0.0.1:9090 |
| Logs | JSON lines on stdout; every API log line has `request_id` (also returned as `X-Request-ID`) |
| Mails | Mailpit http://127.0.0.1:8025 |
| Parked messages | `SELECT * FROM rejected_message ORDER BY received_at DESC LIMIT 20;` |

## Runbooks

### BridgeDown
Prometheus cannot scrape the bridge for 1 minute.
1. Is the process running? `docker compose --profile app ps bridge` (or the terminal running `uv run heatbeat-bridge`).
2. Start it again. Nothing is lost while it is down: the broker keeps the persistent
   session and queues messages (up to `max_queued_messages`, see `mosquitto/mosquitto.conf`).
   This only holds if the broker can write its persistence file (volume `mqtt-data`); see ADR 0010.
3. After the start, `heatbeat_bridge_pending_messages` rises and falls back to near 0 as the backlog drains.
4. If it crashes again, read the last log lines; a crash loop on one message is a bug (garbage is parked, not raised).

### DataFreshnessSLO
The plant with the oldest data has not sent anything for more than 5 minutes.
1. Many plants stale (`heatbeat_plants_stale`) -> central problem: check BridgeDown, BatchWritesFailing, the broker.
2. One plant stale -> that site: gateway offline, network at the building. The "No data" alarm rule tells the operator.
3. Check `rejected_message` for that gateway: an unknown gateway or wrong topic is rejected, not stored.

### BatchWritesFailing
The bridge could not write a batch. Nothing was acknowledged, so the broker keeps the messages.
1. Database reachable? `docker compose ps db`, `pg_isready -h 127.0.0.1`.
2. Disk full? Compression and retention policies should keep it small: `SELECT * FROM timescaledb_information.jobs;`
3. When the database is back the bridge writes the backlog itself (backoff with jitter, max 30 s). No manual replay.

### IngestLagHigh
p95 time from measurement to commit above 30 s.
1. Backlog after an outage? Lag falls by itself; watch `heatbeat_bridge_pending_messages`.
2. Many late uploads into old, compressed chunks are about 5x slower (`docs/measurements.md`).
3. Constantly high without an outage: write time (`heatbeat_bridge_batch_write_seconds`) growing means the
   database is the bottleneck; run a second bridge with a shared subscription (ADR 0003).

### GatewayClockDrift
A gateway's clock differs from the server's by more than 60 s.
1. Find it: `topk(5, abs(heatbeat_gateway_clock_offset_seconds))`.
2. Its data is stored with its own (wrong) timestamps; readings more than 5 min in the future are rejected.
3. Fix NTP on that gateway. Data from the drift period may need correcting.

### NotificationsDead
A notification failed `HB_NOTIFICATION_MAX_ATTEMPTS` times.
1. `SELECT channel, last_error, count(*) FROM notification_outbox WHERE status = 'dead' GROUP BY 1, 2;`
2. Fix the receiver (SMTP, webhook URL). Then requeue:
   `UPDATE notification_outbox SET status = 'pending', attempts = 0, next_attempt_at = now() WHERE status = 'dead';`
   Safe: every notification carries an idempotency key, receivers drop duplicates.

### NotificationBacklog
More than 50 notifications pending for 10 minutes.
1. Is the worker running, is beat running (the notify task is scheduled every 10 s)?
2. Redis up? `redis-cli ping`. Run one round by hand: `uv run heatbeat-job notify`.

### ApiErrorRate
More than 1 % of API requests end in a 5xx.
1. Look at the API logs for `"level": "ERROR"`; use the `request_id` to follow one request.
2. `/ready` returns 503 when the database is unreachable.

### ApiLatencyHigh
p95 above 500 ms.
1. Which route? `histogram_quantile(0.95, sum by (le, route) (rate(heatbeat_http_request_seconds_bucket[5m])))`.
2. Series queries over long ranges should use the hourly aggregate (bucket >= 1 h);
   `docs/query-plans.md` shows the expected plans.

### CommandsExpiring
A parameter change expired in the last 24 h (the gateway never answered in time).
1. `SELECT plant_id, datapoint_id, created_at, expires_at FROM command WHERE state = 'expired' ORDER BY created_at DESC;`
2. Gateway offline? Check its plant's freshness. The gateway never applies an expired command,
   so nothing changed on site. If it answers late with "applied", the platform accepts it (ADR 0008).
3. Ask the user to submit the change again when the gateway is back.

## Routine tasks

| Task | How |
|---|---|
| Migrate | `uv run alembic upgrade head` (each migration also has a tested downgrade) |
| Create demo data | `uv run heatbeat-seed` |
| Run a job by hand | `uv run heatbeat-job alarms|notify|expire|weather|prices|optimiser|measure` |
| Chaos test | `uv run python scripts/chaos.py` (Docker) |
| Backup | `pg_dump` / TimescaleDB-aware backup; not automated in this project |
