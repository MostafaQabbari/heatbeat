"""Tests for the gateways' store-and-forward publisher (ADR 0010), with a fake MQTT client.

What this file does: proves without a broker that a message only counts as sent
after its PUBACK, survives a disconnect, is sent again after a reconnect without
a session, and that a late PUBACK for an old copy is not counted twice. It also
checks the broker configuration keeps sessions on disk (the chaos-test finding).
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import paho.mqtt.client as mqtt

from heatbeat.sim.publisher import ReliablePublisher

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    """Just enough of paho's client: publish() hands out message ids, tests fire the callbacks."""

    def __init__(self) -> None:
        self.connected = True
        self.fail_next = False
        self.sent: list[tuple[int, str]] = []
        self.next_mid = 1
        self.on_publish: Any = None
        self.on_connect: Any = None

    def is_connected(self) -> bool:
        return self.connected

    def publish(self, topic: str, payload: str, qos: int) -> SimpleNamespace:
        assert qos == 1
        if self.fail_next:
            self.fail_next = False
            return SimpleNamespace(rc=mqtt.MQTT_ERR_NO_CONN, mid=0)
        mid, self.next_mid = self.next_mid, self.next_mid + 1
        self.sent.append((mid, payload))
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS, mid=mid)

    def puback(self, mid: int) -> None:
        self.on_publish(self, None, mid, None, None)

    def reconnect(self, session_present: bool) -> None:
        self.connected = True
        self.on_connect(self, None, SimpleNamespace(session_present=session_present), None, None)


def make() -> tuple[FakeClient, ReliablePublisher]:
    client = FakeClient()
    return client, ReliablePublisher(client)  # type: ignore[arg-type]


def test_a_message_counts_as_sent_only_after_its_puback() -> None:
    client, out = make()
    out.publish("t", "a")
    assert out.pending == 1 and out.confirmed == 0
    client.puback(1)
    assert out.pending == 0 and out.confirmed == 1


def test_a_puback_that_arrives_before_the_id_is_recorded_still_counts() -> None:
    _client, out = make()
    out._confirmed_early.add(1)  # what happens when the network thread is faster than ours
    out.publish("t", "a")
    assert out.pending == 0 and out.confirmed == 1


def test_while_disconnected_messages_stay_in_our_buffer() -> None:
    client, out = make()
    client.connected = False
    out.publish("t", "a")
    out.publish("t", "b")
    assert client.sent == [] and out.pending == 2
    client.connected = True
    out.drain()
    assert [p for _, p in client.sent] == ["a", "b"]


def test_a_refused_publish_is_kept_and_tried_again() -> None:
    client, out = make()
    client.fail_next = True
    out.publish("t", "a")
    assert client.sent == [] and out.pending == 1
    out.drain()
    assert [p for _, p in client.sent] == ["a"]


def test_reconnect_without_session_sends_unconfirmed_messages_again() -> None:
    client, out = make()
    out.publish("t", "a")
    out.publish("t", "b")
    client.puback(1)  # "a" confirmed, "b" not
    client.connected = False
    client.reconnect(session_present=False)
    out.drain()
    assert [p for _, p in client.sent] == ["a", "b", "b"]
    assert out.resent_after_reconnect == 1
    client.puback(2)  # late PUBACK for the OLD copy of "b": not counted
    assert out.confirmed == 1 and out.pending == 1
    client.puback(3)  # the new copy
    assert out.confirmed == 2 and out.pending == 0


def test_reconnect_with_session_does_not_resend() -> None:
    client, out = make()
    out.publish("t", "a")
    client.reconnect(session_present=True)  # the broker still has it; the library finishes it
    out.drain()
    assert len(client.sent) == 1 and out.resent_after_reconnect == 0


def test_the_broker_keeps_sessions_on_disk() -> None:
    """Chaos-test finding: without a writable persistence store a broker restart forgets the
    bridge's subscription, and messages confirmed to gateways go to nobody."""
    conf = (ROOT / "mosquitto" / "mosquitto.conf").read_text(encoding="utf-8")
    assert "persistence true" in conf
    assert "persistence_location /mosquitto/data/" in conf
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mqtt-data:/mosquitto/data" in compose
