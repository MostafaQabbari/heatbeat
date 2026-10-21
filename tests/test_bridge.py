"""Tests for batching and acknowledgement (src/heatbeat/bridge.py), with fakes instead of MQTT and DB.

The key promise: a message is acknowledged to the broker ONLY after the database committed it.
"""

import random

from helpers import as_bytes, make_message, topic_for

from heatbeat.bridge import Backoff, Ingestor


class FakeSink:
    """Records batches; can be told to fail like a database that is down."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.batches: list[tuple[list, list]] = []

    def write(self, messages, rejections) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("database down")
        self.batches.append((messages, rejections))
        return sum(len(msg.readings) for _, msg in messages)


class AckRecorder:
    def __init__(self) -> None:
        self.acked: list[int] = []

    def for_message(self, n: int):
        return lambda: self.acked.append(n)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def add_messages(ingestor: Ingestor, acks: AckRecorder, count: int) -> None:
    for n in range(1, count + 1):
        msg = make_message(seq=n)
        ingestor.add(topic_for(msg), as_bytes(msg), ack=acks.for_message(n))


def test_messages_are_acked_after_successful_write():
    sink, acks = FakeSink(), AckRecorder()
    ingestor = Ingestor(sink, batch_size=10, max_wait_s=1)
    add_messages(ingestor, acks, 3)

    assert ingestor.flush() is True

    assert acks.acked == [1, 2, 3]
    assert len(sink.batches) == 1
    assert ingestor.stats["rows_inserted"] == 6


def test_s02_nothing_is_acked_while_database_is_down():
    """S02: the write fails, so the broker must keep the messages (no ack, nothing lost)."""
    sink, acks = FakeSink(fail_times=2), AckRecorder()
    ingestor = Ingestor(sink, batch_size=10, max_wait_s=1)
    add_messages(ingestor, acks, 3)

    assert ingestor.flush() is False
    assert ingestor.flush() is False

    assert acks.acked == []
    assert ingestor.pending_count == 3
    assert ingestor.stats["write_failures"] == 2


def test_s02_after_recovery_each_message_is_acked_exactly_once():
    sink, acks = FakeSink(fail_times=1), AckRecorder()
    ingestor = Ingestor(sink, batch_size=10, max_wait_s=1)
    add_messages(ingestor, acks, 3)

    ingestor.flush()  # fails
    ingestor.flush()  # database is back
    ingestor.flush()  # nothing left

    assert acks.acked == [1, 2, 3]
    assert ingestor.pending_count == 0


def test_s04_no_ack_happens_while_a_message_is_only_received():
    """S04: receiving a message is not handling it; the ack waits for the commit."""
    sink, acks = FakeSink(), AckRecorder()
    ingestor = Ingestor(sink, batch_size=10, max_wait_s=1)

    add_messages(ingestor, acks, 1)

    assert acks.acked == []
    assert sink.batches == []


def test_bad_message_is_stored_as_rejection_and_acked():
    """A poison message must not block the queue: store it with a reason, then ack."""
    sink, acks = FakeSink(), AckRecorder()
    ingestor = Ingestor(sink, batch_size=10, max_wait_s=1)
    ingestor.add("hb/v1/gw-1/plants/p-1/telemetry", b"garbage", ack=acks.for_message(1))

    ingestor.flush()

    messages, rejections = sink.batches[0]
    assert messages == []
    assert len(rejections) == 1
    assert acks.acked == [1]


def test_flush_when_batch_is_full():
    ingestor = Ingestor(FakeSink(), batch_size=3, max_wait_s=60, clock=FakeClock())
    add_messages(ingestor, AckRecorder(), 2)
    assert ingestor.should_flush() is False

    add_messages(ingestor, AckRecorder(), 1)

    assert ingestor.should_flush() is True


def test_flush_when_oldest_message_waited_long_enough():
    clock = FakeClock()
    ingestor = Ingestor(FakeSink(), batch_size=100, max_wait_s=1.0, clock=clock)
    add_messages(ingestor, AckRecorder(), 1)
    assert ingestor.should_flush() is False

    clock.now = 1.5

    assert ingestor.should_flush() is True


def test_empty_ingestor_never_flushes():
    assert Ingestor(FakeSink(), batch_size=1, max_wait_s=0.1).should_flush() is False


def test_s13_backoff_grows_has_a_cap_and_resets():
    backoff = Backoff(base_s=1, cap_s=8, rng=random.Random(1))

    delays = [backoff.next_delay() for _ in range(6)]

    ceilings = [1, 2, 4, 8, 8, 8]
    for delay, ceiling in zip(delays, ceilings, strict=True):
        assert ceiling / 2 <= delay <= ceiling  # jitter stays inside the window
    backoff.reset()
    assert backoff.next_delay() <= 1
