"""MQTT bridge: telemetry from the broker into the database.

What this file does: subscribes to all telemetry topics, collects messages into
batches, writes each batch in one transaction, and only THEN acknowledges the
messages to the broker. If the database is down, nothing is acknowledged, so
the broker keeps the messages and stops sending more (backpressure). A crash
therefore means redelivery, never loss; the unique key makes redelivery harmless.

Run it with:  uv run heatbeat-bridge
"""

import logging
import queue
import random
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from heatbeat.config import Settings
from heatbeat.contract import TELEMETRY_SUBSCRIPTION, TelemetryMessage
from heatbeat.db import PostgresSink
from heatbeat.ingest import Rejection, SeqStatus, SequenceTracker, decode

log = logging.getLogger("heatbeat.bridge")


class Sink(Protocol):
    def write(self, messages: list[tuple[str, TelemetryMessage]], rejections: list[Rejection]) -> int: ...


@dataclass(frozen=True)
class Pending:
    topic: str
    decoded: TelemetryMessage | Rejection
    ack: Callable[[], None]


class Ingestor:
    """Batching and acknowledgement logic, independent of MQTT (so it is testable)."""

    def __init__(
        self,
        sink: Sink,
        batch_size: int,
        max_wait_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._batch_size = batch_size
        self._max_wait_s = max_wait_s
        self._clock = clock
        self._pending: list[Pending] = []
        self._oldest_at = 0.0
        self._sequences = SequenceTracker()
        self.stats: Counter[str] = Counter()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def add(self, topic: str, payload: bytes, ack: Callable[[], None]) -> None:
        decoded = decode(topic, payload)
        if isinstance(decoded, TelemetryMessage):
            status = self._sequences.observe(decoded.gateway, decoded.boot_id, decoded.seq)
            self.stats[f"seq_{status}"] += 1
            if status in (SeqStatus.GAP, SeqStatus.RESTART):
                log.warning("gateway %s: sequence %s at seq=%s", decoded.gateway, status, decoded.seq)
        if not self._pending:
            self._oldest_at = self._clock()
        self._pending.append(Pending(topic, decoded, ack))

    def should_flush(self) -> bool:
        if not self._pending:
            return False
        full = len(self._pending) >= self._batch_size
        waited = self._clock() - self._oldest_at >= self._max_wait_s
        return full or waited

    def flush(self) -> bool:
        """Write all pending messages. Acknowledge them only if the write committed."""
        if not self._pending:
            return True
        messages = [(p.topic, p.decoded) for p in self._pending if isinstance(p.decoded, TelemetryMessage)]
        rejections = [p.decoded for p in self._pending if isinstance(p.decoded, Rejection)]
        try:
            inserted = self._sink.write(messages, rejections)
        except Exception:
            self.stats["write_failures"] += 1
            log.exception("batch write failed; %d messages stay unacknowledged", len(self._pending))
            return False

        for pending in self._pending:
            pending.ack()
        self.stats["messages"] += len(self._pending)
        self.stats["rows_inserted"] += inserted
        self.stats["rejected_messages"] += len(rejections)
        self._pending.clear()
        return True


class Backoff:
    """Exponential backoff with jitter, so many workers do not retry in lockstep."""

    def __init__(self, base_s: float = 0.5, cap_s: float = 30.0, rng: random.Random | None = None) -> None:
        self._base = base_s
        self._cap = cap_s
        self._rng = rng or random.Random()
        self._attempt = 0

    def next_delay(self) -> float:
        ceiling = min(self._cap, self._base * 2**self._attempt)
        self._attempt += 1
        return self._rng.uniform(ceiling / 2, ceiling)

    def reset(self) -> None:
        self._attempt = 0


def ack_later(client: mqtt.Client, msg: mqtt.MQTTMessage) -> Callable[[], None]:
    """Build the function that acknowledges this one message to the broker."""

    def ack() -> None:
        client.ack(msg.mid, msg.qos)

    return ack


def make_client(
    settings: Settings,
    inbox: "queue.Queue[mqtt.MQTTMessage]",
    subscribed: threading.Event | None = None,
) -> mqtt.Client:
    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=settings.mqtt_client_id,
        protocol=mqtt.MQTTv5,
        manual_ack=True,  # we decide when a message counts as handled
    )

    # Handlers are installed BEFORE connecting, so no message can arrive unhandled (S04).
    def on_message(_client: mqtt.Client, _userdata: object, msg: mqtt.MQTTMessage) -> None:
        inbox.put(msg)

    def on_connect(c: mqtt.Client, _u: object, _flags: object, reason: object, _props: object) -> None:
        log.info("connected to broker (%s); subscribing", reason)
        c.subscribe(TELEMETRY_SUBSCRIPTION, qos=1)  # re-subscribes after every reconnect

    def on_subscribe(_c: mqtt.Client, _u: object, _mid: int, reasons: object, _props: object) -> None:
        log.info("subscribed to %s (%s)", TELEMETRY_SUBSCRIPTION, reasons)
        if subscribed is not None:
            subscribed.set()  # lets tests wait for a real signal instead of sleeping

    client.on_message = on_message
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    return client


def run(
    settings: Settings,
    stop: threading.Event,
    sink: "PostgresSink | None" = None,
    subscribed: threading.Event | None = None,
) -> None:
    sink = sink or PostgresSink(settings.database_url)
    ingestor = Ingestor(sink, settings.batch_size, settings.batch_max_wait_s)
    inbox: queue.Queue[mqtt.MQTTMessage] = queue.Queue()
    client = make_client(settings, inbox, subscribed)

    # Persistent session: while we are away, the broker queues our QoS 1 messages.
    props = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
    props.SessionExpiryInterval = 24 * 3600
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30, clean_start=False, properties=props)
    client.loop_start()

    backoff = Backoff()
    last_report = time.monotonic()
    try:
        while not stop.is_set():
            try:
                msg = inbox.get(timeout=0.2)
                ingestor.add(msg.topic, msg.payload, ack=ack_later(client, msg))
            except queue.Empty:
                pass

            if ingestor.should_flush():
                if ingestor.flush():
                    backoff.reset()
                else:
                    stop.wait(backoff.next_delay())

            if time.monotonic() - last_report > 10:
                log.info("stats %s pending=%d", dict(ingestor.stats), ingestor.pending_count)
                last_report = time.monotonic()
    finally:
        # Graceful shutdown: try to store what we have, then leave.
        ingestor.flush()
        client.disconnect()
        client.loop_stop()
        sink.close()
        log.info("bridge stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run(Settings(), stop)


if __name__ == "__main__":
    main()
