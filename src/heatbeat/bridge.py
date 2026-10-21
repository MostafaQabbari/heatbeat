"""MQTT bridge: the only service that talks to the broker.

What this file does:
1. Telemetry in: collects messages into batches, writes each batch in one
   transaction, and only THEN acknowledges them to the broker. If the database
   is down, nothing is acknowledged, so the broker keeps the messages and stops
   sending more (backpressure). A crash means redelivery, never loss; the
   unique key makes redelivery harmless.
2. Commands out: every second, publishes 'requested' commands to their
   gateway (QoS 1, never retained, with an expiry).
3. Command results in: records what the gateway did (and the audit row),
   again acknowledging only after the commit.
It also exposes Prometheus metrics on port 9101.

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
from datetime import UTC, datetime
from typing import Protocol

import paho.mqtt.client as mqtt
import psycopg
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from prometheus_client import start_http_server
from pydantic import ValidationError

from heatbeat import observability as obs
from heatbeat.commands import CommandStore
from heatbeat.config import Settings
from heatbeat.contract import (
    COMMAND_RESULT_SUBSCRIPTION,
    TELEMETRY_SUBSCRIPTION,
    CommandMessage,
    CommandResult,
    TelemetryMessage,
    command_topic,
    gateway_from_topic,
)
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
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sink = sink
        self._batch_size = batch_size
        self._max_wait_s = max_wait_s
        self._clock = clock
        self._wall_clock = wall_clock
        self._pending: list[Pending] = []
        self._oldest_at = 0.0
        self._sequences = SequenceTracker()
        self.stats: Counter[str] = Counter()
        self.drifting_gateways: set[str] = set()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def add(self, topic: str, payload: bytes, ack: Callable[[], None]) -> None:
        decoded = decode(topic, payload)
        if isinstance(decoded, TelemetryMessage):
            self._observe(decoded)
        if not self._pending:
            self._oldest_at = self._clock()
        self._pending.append(Pending(topic, decoded, ack))
        obs.PENDING.set(len(self._pending))

    def _observe(self, msg: TelemetryMessage) -> None:
        status = self._sequences.observe(msg.gateway, msg.boot_id, msg.seq)
        self.stats[f"seq_{status}"] += 1
        obs.SEQUENCE.labels(status.value).inc()
        if status in (SeqStatus.GAP, SeqStatus.RESTART):
            log.warning("gateway %s: sequence %s at seq=%s", msg.gateway, status, msg.seq)

        now = self._wall_clock()
        offset = obs.clock_offset_s(msg.sent_at, now)
        obs.CLOCK_OFFSET.labels(msg.gateway).set(offset)
        if obs.clock_drifting(offset):
            if msg.gateway not in self.drifting_gateways:
                log.warning("gateway %s: clock off by %.0f s (S21)", msg.gateway, offset)
            self.drifting_gateways.add(msg.gateway)
        else:
            self.drifting_gateways.discard(msg.gateway)
        newest = max(r.t for r in msg.readings)
        obs.LAG_SECONDS.observe(max(0.0, (now - newest).total_seconds()))

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
        started = time.perf_counter()
        try:
            inserted = self._sink.write(messages, rejections)
        except Exception:
            self.stats["write_failures"] += 1
            obs.WRITE_FAILURES.inc()
            log.exception("batch write failed; %d messages stay unacknowledged", len(self._pending))
            return False
        obs.WRITE_SECONDS.observe(time.perf_counter() - started)

        for pending in self._pending:
            pending.ack()
        self.stats["messages"] += len(self._pending)
        self.stats["rows_inserted"] += inserted
        self.stats["rejected_messages"] += len(rejections)
        obs.MESSAGES.labels("stored").inc(len(messages))
        obs.MESSAGES.labels("rejected").inc(len(rejections))
        obs.ROWS.inc(inserted)
        self._pending.clear()
        obs.PENDING.set(0)
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
        # Re-subscribes after every reconnect.
        c.subscribe([(TELEMETRY_SUBSCRIPTION, 1), (COMMAND_RESULT_SUBSCRIPTION, 1)])

    def on_subscribe(_c: mqtt.Client, _u: object, _mid: int, reasons: object, _props: object) -> None:
        log.info("subscribed (%s)", reasons)
        if subscribed is not None:
            subscribed.set()  # lets tests wait for a real signal instead of sleeping

    client.on_message = on_message
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    return client


class CommandLink:
    """Commands out and results in, with its own database connection."""

    def __init__(self, settings: Settings, client: mqtt.Client) -> None:
        self._url = settings.database_url
        self._client = client
        self._conn: psycopg.Connection | None = None
        self.retry: list[tuple[CommandResult, Callable[[], None]]] = []

    def _store(self) -> CommandStore:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._url, autocommit=True)
        return CommandStore(self._conn)

    def _publish(self, gateway: str, message: CommandMessage) -> bool:
        info = self._client.publish(command_topic(gateway), message.model_dump_json(by_alias=True), qos=1)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        if ok:
            obs.COMMANDS_SENT.inc()
        return ok

    def publish_due(self) -> None:
        try:
            sent = self._store().publish_due(self._publish, datetime.now(UTC))
            if sent:
                log.info("published %d command(s)", sent)
        except psycopg.Error:
            log.warning("could not publish commands (database?)", exc_info=True)
            self.close()

    def on_result(self, msg: mqtt.MQTTMessage, ack: Callable[[], None]) -> None:
        gateway = gateway_from_topic(msg.topic, "command-results")
        try:
            result = CommandResult.model_validate_json(msg.payload)
        except ValidationError:
            log.warning("invalid command result on %s; dropped", msg.topic)
            ack()
            return
        if result.gateway != gateway:
            log.warning("result from %s claims gateway %s; dropped", gateway, result.gateway)
            ack()
            return
        self.retry.append((result, ack))
        self.process_results()

    def process_results(self) -> None:
        while self.retry:
            result, ack = self.retry[0]
            try:
                self._store().record_result(result)
            except psycopg.Error:
                log.warning("could not record command result; will retry", exc_info=True)
                self.close()
                return
            ack()  # only after the commit
            obs.COMMAND_RESULTS.labels(result.status).inc()
            self.retry.pop(0)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def run(
    settings: Settings,
    stop: threading.Event,
    sink: PostgresSink | None = None,
    subscribed: threading.Event | None = None,
) -> None:
    sink = sink or PostgresSink(settings.database_url)
    ingestor = Ingestor(sink, settings.batch_size, settings.batch_max_wait_s)
    inbox: queue.Queue[mqtt.MQTTMessage] = queue.Queue()
    client = make_client(settings, inbox, subscribed)
    commands = CommandLink(settings, client)

    # Persistent session: while we are away, the broker queues our QoS 1 messages.
    props = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
    props.SessionExpiryInterval = 24 * 3600
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30, clean_start=False, properties=props)
    client.loop_start()

    backoff = Backoff()
    last_report = last_commands = time.monotonic()
    try:
        while not stop.is_set():
            try:
                msg = inbox.get(timeout=0.2)
                if msg.topic.endswith("/command-results"):
                    commands.on_result(msg, ack_later(client, msg))
                else:
                    ingestor.add(msg.topic, msg.payload, ack=ack_later(client, msg))
            except queue.Empty:
                pass

            if ingestor.should_flush():
                if ingestor.flush():
                    backoff.reset()
                else:
                    stop.wait(backoff.next_delay())

            if time.monotonic() - last_commands >= 1.0:
                commands.process_results()
                commands.publish_due()
                last_commands = time.monotonic()

            if time.monotonic() - last_report > 10:
                log.info("stats %s pending=%d", dict(ingestor.stats), ingestor.pending_count)
                last_report = time.monotonic()
    finally:
        # Graceful shutdown: try to store what we have, then leave.
        ingestor.flush()
        client.disconnect()
        client.loop_stop()
        sink.close()
        commands.close()
        log.info("bridge stopped")


def main() -> None:
    obs.setup_logging()
    settings = Settings()
    # Same listen address as the API: this computer only, unless Docker sets 0.0.0.0.
    start_http_server(settings.bridge_metrics_port, addr=settings.api_host)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run(settings, stop)


if __name__ == "__main__":
    main()
