"""Tests for the notification channels and backoff (src/heatbeat/notifier.py), scenario S13.

The outbox processing itself is tested against a real database in test_integration_alarms.py.
"""

import random
import socket

import httpx2
import pytest

from heatbeat.notifier import (
    EmailChannel,
    FakeSmsChannel,
    Notification,
    PermanentError,
    TemporaryError,
    WebhookChannel,
    backoff_delay,
)

N = Notification(1, "webhook", "https://tickets.example.test/hook", "[HIGH] x", "body", "alarm-event-1-webhook")


def webhook(status: int, seen: list[httpx2.Request] | None = None) -> WebhookChannel:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status)

    return WebhookChannel(httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_webhook_sends_the_idempotency_key():
    """S14: the receiver can recognise a second copy of the same notification."""
    seen: list[httpx2.Request] = []

    webhook(200, seen).send(N)

    assert seen[0].headers["Idempotency-Key"] == "alarm-event-1-webhook"


@pytest.mark.parametrize("status", [500, 502, 503, 408, 429])
def test_s13_receiver_trouble_is_temporary(status):
    with pytest.raises(TemporaryError):
        webhook(status).send(N)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_bad_request_is_permanent(status):
    with pytest.raises(PermanentError):
        webhook(status).send(N)


def test_network_error_is_temporary():
    def broken(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    with pytest.raises(TemporaryError):
        WebhookChannel(httpx2.Client(transport=httpx2.MockTransport(broken))).send(N)


def test_mail_server_down_is_temporary():
    with socket.socket() as s:  # find a port where nothing listens
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    with pytest.raises(TemporaryError):
        EmailChannel("127.0.0.1", free_port, "alarms@heatbeat.local").send(N)


def test_fake_sms_records_the_message():
    sms = FakeSmsChannel()

    sms.send(N)

    assert sms.sent == [N]


def test_s13_backoff_grows_with_jitter_and_a_cap():
    rng = random.Random(1)
    delays = [backoff_delay(attempt, rng).total_seconds() for attempt in range(1, 12)]

    assert 15 <= delays[0] <= 30
    assert 30 <= delays[1] <= 60
    assert max(delays) <= 3600  # never more than an hour
    assert len(set(delays)) == len(delays)  # jitter: workers do not retry in lockstep
