"""Store-and-forward publishing for gateways: a message counts as sent only when the broker confirmed it.

What this file does: the gateway-side twin of the bridge's "ack after commit".
`client.publish()` returning "success" only means "queued in the library". If the
broker restarts and no longer knows our session, the MQTT 5 specification says the
client must throw its unconfirmed messages away (CONNACK "session present = 0").
So the gateway keeps every message in its own buffer until the PUBACK arrives,
and after a reconnect without a session it sends the unconfirmed ones again.
(Duplicates are harmless: the bridge stores each reading only once.)

Locking rule (the chaos test also found a deadlock here): never call the MQTT
library while holding our own lock. The library calls `_on_publish` while it holds
ITS lock; if we held ours while calling `publish()`, each thread would wait for
the other forever (lock-order inversion).
"""

import threading
import time
from collections.abc import Callable
from typing import Any, cast

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties


def persistent_session() -> Properties:
    """CONNECT properties for a session the broker keeps for one hour after a disconnect."""
    props = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
    props.SessionExpiryInterval = 3600
    return props


class ReliablePublisher:
    def __init__(self, client: mqtt.Client) -> None:
        self.client = client
        self._lock = threading.Lock()  # protects the buffers below; never held while calling the library
        self._drain_lock = threading.Lock()  # one sender at a time; never taken inside library callbacks
        self._waiting: list[tuple[str, str]] = []  # not handed to the library yet
        self._unconfirmed: dict[int, tuple[str, str]] = {}  # handed over, PUBACK not received yet
        self._confirmed_early: set[int] = set()  # PUBACK arrived before we recorded the message id
        self._abandoned: set[int] = set()  # ids we re-sent under a new id; a late PUBACK for them is ignored
        self.confirmed = 0
        self.resent_after_reconnect = 0
        # paho's callback type is a union of signatures; we pass through whatever was set before.
        self._chained_on_connect = cast(Callable[..., Any] | None, client.on_connect)
        client.on_publish = self._on_publish
        client.on_connect = self._on_connect

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._waiting) + len(self._unconfirmed)

    def publish(self, topic: str, payload: str) -> None:
        with self._lock:
            self._waiting.append((topic, payload))
        self.drain()

    def wait_until_confirmed(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if self.pending == 0:
                return True
            time.sleep(0.1)
        return False

    def drain(self) -> None:
        """Hand waiting messages to the library (only while connected)."""
        with self._drain_lock:
            if not self.client.is_connected():
                return  # keep everything; the library would drop it on a clean reconnect
            with self._lock:
                batch, self._waiting = self._waiting, []
            failed = []
            for topic, payload in batch:
                info = self.client.publish(topic, payload, qos=1)  # no lock of ours is held here
                with self._lock:
                    if info.rc != mqtt.MQTT_ERR_SUCCESS:
                        failed.append((topic, payload))
                    elif info.mid in self._confirmed_early:
                        self._confirmed_early.discard(info.mid)
                        self.confirmed += 1
                    else:
                        self._unconfirmed[info.mid] = (topic, payload)
            with self._lock:
                self._waiting = failed + self._waiting

    def _on_publish(self, _c: mqtt.Client, _u: object, mid: int, _rc: object, _p: object) -> None:
        with self._lock:
            if self._unconfirmed.pop(mid, None) is not None:
                self.confirmed += 1
            elif mid in self._abandoned:
                self._abandoned.discard(mid)  # the copy under the new id counts, not this one
            else:
                self._confirmed_early.add(mid)

    def _on_connect(self, client: mqtt.Client, userdata: object, flags: object, reason: object, props: object) -> None:
        # Runs on the library's thread: only move buffers here, the next drain() sends them.
        with self._lock:
            if not getattr(flags, "session_present", False) and self._unconfirmed:
                # The broker forgot us: everything unconfirmed must be sent again.
                # (Some libraries, paho included, also resend their own copy; duplicates are harmless.)
                self.resent_after_reconnect += len(self._unconfirmed)
                self._abandoned.update(self._unconfirmed)
                self._waiting = list(self._unconfirmed.values()) + self._waiting
                self._unconfirmed.clear()
                self._confirmed_early.clear()
        if self._chained_on_connect is not None:
            self._chained_on_connect(client, userdata, flags, reason, props)
