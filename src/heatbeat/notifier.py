"""Notification sender: reads the outbox and delivers e-mail, webhook and SMS.

What this file does: every few seconds it takes due outbox rows (FOR UPDATE
SKIP LOCKED, so several workers can share the work), sends each one, and marks
it sent. Temporary failures (receiver down, timeout) are retried with
exponential backoff and jitter; after `max_attempts`, or on a permanent error,
the row is "dead" and waits for a person (S13). Every message carries an
idempotency key, so if we crash after sending but before marking it sent, the
second copy can be recognised and ignored by the receiver (S14). Messages of
one alarm and channel go out in order: "fired" before "cleared".
"""

import logging
import random
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol

import httpx2
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("heatbeat.notifier")


class TemporaryError(Exception):
    """Worth retrying later (receiver down, timeout, 5xx)."""


class PermanentError(Exception):
    """Retrying will not help (bad address, 4xx)."""


@dataclass(frozen=True)
class Notification:
    id: int
    channel: str
    recipient: str
    subject: str
    body: str
    idempotency_key: str


class Channel(Protocol):
    def send(self, n: Notification) -> None: ...


class EmailChannel:
    def __init__(self, host: str, port: int, sender: str) -> None:
        self.host, self.port, self.sender = host, port, sender

    def send(self, n: Notification) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, n.recipient, n.subject
        msg["Message-ID"] = f"<{n.idempotency_key}@heatbeat>"  # same id on a resend
        msg.set_content(n.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
                smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentError(str(exc)) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise TemporaryError(str(exc)) from exc


class WebhookChannel:
    """POSTs JSON to a ticket system or chat hook."""

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self.client = client or httpx2.Client(timeout=10)

    def send(self, n: Notification) -> None:
        try:
            response = self.client.post(
                n.recipient,
                json={"subject": n.subject, "body": n.body, "id": n.idempotency_key},
                headers={"Idempotency-Key": n.idempotency_key},
            )
        except httpx2.HTTPError as exc:
            raise TemporaryError(str(exc)) from exc
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise TemporaryError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PermanentError(f"HTTP {response.status_code}")


@dataclass
class FakeSmsChannel:
    """Real SMS providers cost money; this one only logs. Same interface as a real one."""

    sent: list[Notification] = field(default_factory=list)

    def send(self, n: Notification) -> None:
        self.sent.append(n)
        log.info("SMS (fake) to %s: %s", n.recipient, n.subject)


def backoff_delay(attempt: int, rng: random.Random, base_s: float = 30, cap_s: float = 3600) -> timedelta:
    """Attempt 1 -> about 15-30 s, 2 -> 30-60 s, ... capped at one hour, with jitter."""
    ceiling = min(cap_s, base_s * 2 ** (attempt - 1))
    return timedelta(seconds=rng.uniform(ceiling / 2, ceiling))


@dataclass
class DispatchStats:
    sent: int = 0
    retried: int = 0
    dead: int = 0


DUE_SQL = """
    SELECT o.id, o.channel, o.recipient, o.subject, o.body, o.idempotency_key, o.attempts
    FROM notification_outbox o
    WHERE o.status = 'pending' AND o.next_attempt_at <= %(now)s
      AND NOT EXISTS (  -- keep order within one alarm and channel
          SELECT 1 FROM notification_outbox e
          WHERE e.alarm_id = o.alarm_id AND e.channel = o.channel
            AND e.status = 'pending' AND e.id < o.id)
    ORDER BY o.id
    LIMIT %(limit)s
    FOR UPDATE OF o SKIP LOCKED
"""


def dispatch_due(
    conn: psycopg.Connection,
    channels: dict[str, Channel],
    now: datetime,
    max_attempts: int,
    rng: random.Random | None = None,
    limit: int = 50,
) -> DispatchStats:
    rng = rng or random.Random()
    stats = DispatchStats()
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        rows: list[dict[str, Any]] = cur.execute(DUE_SQL, {"now": now, "limit": limit}).fetchall()
        for row in rows:
            attempts = row.pop("attempts") + 1
            n = Notification(**row)
            channel = channels.get(n.channel)
            try:
                if channel is None:
                    raise PermanentError(f"channel {n.channel} not configured")
                channel.send(n)
            except TemporaryError as exc:
                if attempts >= max_attempts:
                    _mark(conn, n.id, "dead", attempts, now, str(exc))
                    stats.dead += 1
                else:
                    retry_at = now + backoff_delay(attempts, rng)
                    _mark(conn, n.id, "pending", attempts, retry_at, str(exc))
                    stats.retried += 1
                continue
            except PermanentError as exc:
                _mark(conn, n.id, "dead", attempts, now, str(exc))
                stats.dead += 1
                continue
            conn.execute(
                "UPDATE notification_outbox SET status = 'sent', attempts = %s, sent_at = %s, last_error = NULL "
                "WHERE id = %s",
                (attempts, now, n.id),
            )
            stats.sent += 1
    if stats.sent or stats.retried or stats.dead:
        log.info("notifications: %s", stats)
    return stats


def _mark(conn: psycopg.Connection, nid: int, status: str, attempts: int, next_at: datetime, error: str) -> None:
    conn.execute(
        "UPDATE notification_outbox SET status = %s, attempts = %s, next_attempt_at = %s, last_error = %s "
        "WHERE id = %s",
        (status, attempts, next_at, error[:500], nid),
    )
    if status == "dead":
        log.warning("notification %s parked as dead after %s attempts: %s", nid, attempts, error)
