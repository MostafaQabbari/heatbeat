"""Tests for metrics, logs and the clock-drift check (src/heatbeat/observability.py), scenario S21."""

import json
import logging
from datetime import UTC, datetime, timedelta

from helpers import as_bytes, make_message, topic_for

from heatbeat.bridge import Ingestor
from heatbeat.observability import JsonFormatter, clock_drifting, clock_offset_s, request_id

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_clock_offset_sign():
    assert clock_offset_s(NOW + timedelta(seconds=90), NOW) == 90  # gateway runs ahead
    assert clock_offset_s(NOW - timedelta(seconds=30), NOW) == -30


def test_drift_threshold():
    assert not clock_drifting(45)
    assert clock_drifting(-120)


class NullSink:
    def write(self, messages, rejections) -> int:
        return 0


def test_s21_bridge_notices_a_gateway_with_a_wrong_clock():
    """A gateway whose 'sent_at' is 10 minutes ahead of the server is flagged."""
    ingestor = Ingestor(NullSink(), batch_size=10, max_wait_s=1, wall_clock=lambda: NOW)
    ok, skewed = make_message(gateway="gw-ok", at=NOW), make_message(gateway="gw-bad", at=NOW + timedelta(minutes=10))

    for msg in (ok, skewed):
        ingestor.add(topic_for(msg), as_bytes(msg), ack=lambda: None)

    assert ingestor.drifting_gateways == {"gw-bad"}


def test_drift_flag_clears_when_the_clock_is_fixed():
    ingestor = Ingestor(NullSink(), batch_size=10, max_wait_s=1, wall_clock=lambda: NOW)
    for at in (NOW + timedelta(minutes=10), NOW):
        msg = make_message(gateway="gw-x", at=at, seq=1 if at > NOW else 2)
        ingestor.add(topic_for(msg), as_bytes(msg), ack=lambda: None)

    assert ingestor.drifting_gateways == set()


def test_json_log_line_carries_the_request_id():
    token = request_id.set("req-123")
    try:
        record = logging.LogRecord("heatbeat.api", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id.reset(token)

    assert line["msg"] == "hello world"
    assert line["request_id"] == "req-123"
