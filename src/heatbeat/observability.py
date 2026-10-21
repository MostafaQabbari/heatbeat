"""Metrics and structured logs.

What this file does:
- Prometheus metrics for the bridge (throughput, write time, lag from device
  time to storage, sequence gaps, gateway clock offset) and the API (request
  count and duration per route)
- state metrics computed from the database at scrape time (data freshness,
  firing alarms, notification backlog, open commands), so they stay correct
  even when the process that produces the data is down
- JSON log lines with a request id, so one request can be followed across logs
- the clock-drift check for gateways (S21)
"""

import contextvars
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

CLOCK_DRIFT_LIMIT_S = 60.0
request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# --- bridge -----------------------------------------------------------------
MESSAGES = Counter("heatbeat_bridge_messages_total", "MQTT messages handled", ["result"])
ROWS = Counter("heatbeat_bridge_rows_inserted_total", "New measurement rows stored")
WRITE_FAILURES = Counter("heatbeat_bridge_write_failures_total", "Batch writes that failed (nothing acked)")
WRITE_SECONDS = Histogram("heatbeat_bridge_batch_write_seconds", "Time to write one batch")
LAG_SECONDS = Histogram(
    "heatbeat_ingest_lag_seconds",
    "Receive time minus device time of the newest reading in a message",
    buckets=(1, 2, 5, 10, 30, 60, 300, 900, 3600, 6 * 3600, 24 * 3600),
)
PENDING = Gauge("heatbeat_bridge_pending_messages", "Messages received but not yet stored and acked")
SEQUENCE = Counter("heatbeat_gateway_sequence_events_total", "Sequence checks per gateway message", ["status"])
CLOCK_OFFSET = Gauge("heatbeat_gateway_clock_offset_seconds", "Gateway clock minus server clock", ["gateway"])
COMMANDS_SENT = Counter("heatbeat_commands_sent_total", "Commands published to gateways")
COMMAND_RESULTS = Counter("heatbeat_command_results_total", "Command results from gateways", ["status"])

# --- API --------------------------------------------------------------------
HTTP_REQUESTS = Counter("heatbeat_http_requests_total", "HTTP requests", ["route", "method", "status"])
HTTP_SECONDS = Histogram("heatbeat_http_request_seconds", "HTTP request duration", ["route", "method"])


def clock_offset_s(sent_at: datetime, received_at: datetime) -> float:
    """Positive: the gateway clock runs ahead of ours."""
    return (sent_at - received_at).total_seconds()


def clock_drifting(offset_s: float, limit_s: float = CLOCK_DRIFT_LIMIT_S) -> bool:
    return abs(offset_s) > limit_s


# --- state metrics from the database ----------------------------------------
STATE_QUERIES = {
    "heatbeat_plants_total": "SELECT count(*) FROM plant",
    "heatbeat_plants_stale": (
        "SELECT count(*) FROM plant WHERE last_seen_at IS NULL OR last_seen_at < now() - INTERVAL '5 minutes'"
    ),
    "heatbeat_data_freshness_max_seconds": (
        "SELECT coalesce(max(extract(epoch FROM now() - last_seen_at)), 0) FROM plant WHERE last_seen_at IS NOT NULL"
    ),
    "heatbeat_alarms_firing": "SELECT count(*) FROM alarm WHERE state = 'firing'",
    "heatbeat_notifications_pending": "SELECT count(*) FROM notification_outbox WHERE status = 'pending'",
    "heatbeat_notifications_dead": "SELECT count(*) FROM notification_outbox WHERE status = 'dead'",
    "heatbeat_commands_open": "SELECT count(*) FROM command WHERE state IN ('pending_approval','requested','sent')",
    "heatbeat_commands_expired_24h": (
        "SELECT count(*) FROM command WHERE state = 'expired' AND result_at > now() - INTERVAL '1 day'"
    ),
}


def state_metrics(conn: object) -> CollectorRegistry:
    """Fresh registry with gauges computed from the database right now."""
    registry = CollectorRegistry()
    for name, sql in STATE_QUERIES.items():
        gauge = Gauge(name, name.replace("_", " "), registry=registry)
        row = conn.execute(sql).fetchone()  # type: ignore[attr-defined]
        gauge.set(float(row[0]) if row else 0.0)
    return registry


# --- logs -------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
        }
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line)


def setup_logging(json_logs: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


# --- API middleware and /metrics --------------------------------------------
def install_http_metrics(app: FastAPI) -> None:
    @app.middleware("http")
    async def measure(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")  # template, not the raw URL: bounded label values
        HTTP_REQUESTS.labels(path, request.method, str(response.status_code)).inc()
        HTTP_SECONDS.labels(path, request.method).observe(time.perf_counter() - started)
        response.headers["X-Request-ID"] = rid
        return response

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def metrics(request: Request) -> Response:
        body = generate_latest(DEFAULT_REGISTRY)
        pool = getattr(request.app.state, "pool", None)
        if pool is not None:
            try:
                with pool.connection() as conn:
                    body += generate_latest(state_metrics(conn))
            except Exception:  # metrics must never break because the DB is down
                logging.getLogger("heatbeat.metrics").warning("state metrics unavailable", exc_info=True)
        return Response(body, media_type=CONTENT_TYPE_LATEST)
