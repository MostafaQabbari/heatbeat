"""Integration tests: alarm evaluation, transactional outbox and notification delivery.

Scenarios: S13 (receiver down: retries, then parked), S14 (crash between sending
and marking as sent: exactly one EFFECTIVE notification), S18 (a second worker
never evaluates an alarm that another worker holds).
"""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from helpers import count, make_message, provision, topic_for

from heatbeat.alarm_engine import evaluate_alarms
from heatbeat.contract import TelemetryMessage
from heatbeat.db import PostgresSink
from heatbeat.notifier import Notification, TemporaryError, dispatch_due

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
SEND = NOW + timedelta(minutes=3)  # dispatch runs after the outbox rows were written


@pytest.fixture
def low_spread_plant(db) -> str:
    """Plant p-17 had a flow/return spread of only 1 K for the last 2 hours, and a rule that watches it."""
    tenant = provision(db, "gw-0042")
    readings = []
    for minute in range(120, 0, -1):
        t = (NOW - timedelta(minutes=minute)).isoformat()
        readings += [{"dp": "flow_temp", "t": t, "v": 50.0}, {"dp": "return_temp", "t": t, "v": 49.0}]
    msg = make_message(readings=readings)
    sink = PostgresSink(db)
    sink.write([(topic_for(msg), TelemetryMessage.model_validate(msg))], [])
    sink.close()
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO alarm_rule (tenant_id, name, kind, datapoint_id, second_datapoint_id, above, threshold,
                                    delay_s, hysteresis, urgency)
            SELECT %s, 'Low spread', 'spread', a.id, b.id, false, 3.0, 1800, 0.5, 'high'
            FROM datapoint a, datapoint b WHERE a.key = 'flow_temp' AND b.key = 'return_temp'
            """,
            (tenant,),
        )
    return db


def run_engine(db: str, test_settings, now: datetime = NOW):
    with psycopg.connect(db, autocommit=True) as conn:
        return evaluate_alarms(conn, test_settings.model_copy(update={"webhook_url": "https://t.example/h"}), now)


def test_condition_held_long_enough_fires_and_queues_notifications(low_spread_plant, test_settings):
    stats = run_engine(low_spread_plant, test_settings)

    assert stats.fired == 1
    assert count(low_spread_plant, "SELECT count(*) FROM alarm WHERE state = 'firing'") == 1
    assert count(low_spread_plant, "SELECT count(*) FROM alarm_event WHERE kind = 'fired'") == 1
    # urgency 'high' -> e-mail AND webhook, written in the same transaction as the event (outbox)
    assert count(low_spread_plant, "SELECT count(*) FROM notification_outbox") == 2


def test_running_again_without_new_data_changes_nothing(low_spread_plant, test_settings):
    run_engine(low_spread_plant, test_settings)
    second = run_engine(low_spread_plant, test_settings, NOW + timedelta(seconds=30))

    assert second.fired == 0
    assert count(low_spread_plant, "SELECT count(*) FROM alarm_event") == 1


def test_s18_alarm_held_by_another_worker_is_skipped(low_spread_plant, test_settings):
    run_engine(low_spread_plant, test_settings)  # creates the alarm row
    with psycopg.connect(low_spread_plant) as other_worker:
        other_worker.execute("SELECT id FROM alarm FOR UPDATE")  # a paused worker still holds the lock

        stats = run_engine(low_spread_plant, test_settings, NOW + timedelta(minutes=5))

        assert stats.skipped_locked == 1
        assert stats.evaluated == 0
        other_worker.rollback()


class Receiver:
    """A ticket system that ignores a second copy with the same idempotency key."""

    def __init__(self) -> None:
        self.received: list[str] = []

    @property
    def effective(self) -> set[str]:
        return set(self.received)


class Recording:
    def __init__(self, receiver: Receiver, fail_with: Exception | None = None) -> None:
        self.receiver, self.fail_with = receiver, fail_with

    def send(self, n: Notification) -> None:
        if isinstance(self.fail_with, TemporaryError):
            raise self.fail_with
        self.receiver.received.append(n.idempotency_key)
        if self.fail_with is not None:
            raise self.fail_with  # the message went out, THEN we crash


class Crash(Exception):
    """Stands in for the worker process dying."""


def dispatch(db: str, channels, now: datetime, max_attempts: int = 3):
    with psycopg.connect(db, autocommit=True) as conn:
        return dispatch_due(conn, channels, now, max_attempts)


def test_notifications_are_sent_and_marked(low_spread_plant, test_settings):
    run_engine(low_spread_plant, test_settings)
    receiver = Receiver()

    stats = dispatch(low_spread_plant, {"email": Recording(receiver), "webhook": Recording(receiver)}, SEND)

    assert stats.sent == 2
    assert count(low_spread_plant, "SELECT count(*) FROM notification_outbox WHERE status = 'sent'") == 2


def test_s13_receiver_down_is_retried_later_then_parked(low_spread_plant, test_settings):
    run_engine(low_spread_plant, test_settings)
    down = Recording(Receiver(), fail_with=TemporaryError("503"))
    channels = {"email": down, "webhook": down}

    first = dispatch(low_spread_plant, channels, SEND)
    too_early = dispatch(low_spread_plant, channels, SEND + timedelta(seconds=1))

    assert first.retried == 2
    assert too_early.retried == 0  # backoff: not tried again immediately
    later = SEND
    for _ in range(5):
        later += timedelta(hours=2)
        dispatch(low_spread_plant, channels, later)
    assert count(low_spread_plant, "SELECT count(*) FROM notification_outbox WHERE status = 'dead'") == 2


def test_s14_crash_after_sending_gives_exactly_one_effective_notification(low_spread_plant, test_settings):
    run_engine(low_spread_plant, test_settings)
    receiver = Receiver()

    with pytest.raises(Crash):
        dispatch(
            low_spread_plant, {"email": Recording(receiver, fail_with=Crash()), "webhook": Recording(receiver)}, SEND
        )
    assert count(low_spread_plant, "SELECT count(*) FROM notification_outbox WHERE status = 'sent'") == 0  # rolled back

    dispatch(low_spread_plant, {"email": Recording(receiver), "webhook": Recording(receiver)}, SEND)

    assert len(receiver.received) == 3  # the e-mail went out twice ...
    assert len(receiver.effective) == 2  # ... but with the same key: one effective e-mail, one webhook


def test_cleared_is_never_sent_before_fired(low_spread_plant, test_settings):
    run_engine(low_spread_plant, test_settings)
    msg = make_message(  # the spread recovers: 10 K for three minutes
        readings=[
            {"dp": dp, "t": (NOW + timedelta(minutes=m)).isoformat(), "v": v}
            for m in range(1, 4)
            for dp, v in (("flow_temp", 50.0), ("return_temp", 40.0))
        ],
        seq=2,
    )
    sink = PostgresSink(low_spread_plant)
    sink.write([(topic_for(msg), TelemetryMessage.model_validate(msg))], [])
    sink.close()
    run_engine(low_spread_plant, test_settings, NOW + timedelta(minutes=5))
    assert count(low_spread_plant, "SELECT count(*) FROM alarm_event WHERE kind = 'cleared'") == 1

    sent: list[str] = []

    class Ordered:
        def send(self, n: Notification) -> None:
            sent.append(n.subject.split(": ")[1].split(" at ")[0])

    dispatch(low_spread_plant, {"email": Ordered(), "webhook": Ordered()}, SEND + timedelta(minutes=5))
    dispatch(low_spread_plant, {"email": Ordered(), "webhook": Ordered()}, SEND + timedelta(minutes=6))

    assert sent.index("FIRED") < sent.index("cleared")
