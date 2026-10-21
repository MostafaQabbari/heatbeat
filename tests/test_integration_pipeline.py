"""End-to-end: publish over real MQTT, the real bridge stores it in real TimescaleDB.

Run with:  uv run pytest -m integration   (needs: docker compose up -d db mqtt)
"""

import os
import threading
import time
import uuid

import paho.mqtt.client as mqtt
import pytest
from helpers import as_bytes, count, make_message, topic_for
from paho.mqtt.enums import CallbackAPIVersion

from heatbeat.bridge import run
from heatbeat.config import Settings
from heatbeat.db import PostgresSink

pytestmark = pytest.mark.integration

MQTT_HOST = os.environ.get("HB_TEST_MQTT_HOST", "127.0.0.1")


def wait_until(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def bridge(db):
    """Start the real bridge in a background thread; stop it after the test."""
    settings = Settings(
        _env_file=None,
        db_password="unused",
        mqtt_host=MQTT_HOST,
        mqtt_client_id=f"test-bridge-{uuid.uuid4().hex[:8]}",
        batch_max_wait_s=0.2,
    )
    stop, subscribed = threading.Event(), threading.Event()
    thread = threading.Thread(target=run, args=(settings, stop, PostgresSink(db), subscribed), daemon=True)
    thread.start()
    # Wait for a real signal, not a fixed sleep (fixed sleeps make tests flaky on slow machines).
    assert subscribed.wait(timeout=20), "bridge did not subscribe within 20 s"
    yield
    stop.set()
    thread.join(timeout=10)


@pytest.fixture
def publisher():
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"test-pub-{uuid.uuid4().hex[:8]}")
    client.connect(MQTT_HOST, 1883)
    client.loop_start()
    yield client
    client.disconnect()
    client.loop_stop()


def publish(client: mqtt.Client, message: dict) -> None:
    client.publish(topic_for(message), as_bytes(message), qos=1).wait_for_publish(timeout=5)


def test_message_travels_from_broker_to_database(bridge, publisher, db):
    publish(publisher, make_message())

    assert wait_until(lambda: count(db, "SELECT count(*) FROM measurement") == 2)


def test_s01_same_message_published_twice_is_stored_once(bridge, publisher, db):
    message = make_message()
    publish(publisher, message)
    publish(publisher, message)
    publish(publisher, make_message(seq=2, plant="p-18"))  # a marker that arrives last

    assert wait_until(lambda: count(db, "SELECT count(*) FROM plant WHERE key = 'p-18'") == 1)
    assert count(db, "SELECT count(*) FROM measurement WHERE plant_id = 1") == 2


def test_garbage_is_parked_and_does_not_block_good_messages(bridge, publisher, db):
    publisher.publish("hb/v1/gw-1/plants/p-1/telemetry", b"not json", qos=1).wait_for_publish(5)
    publish(publisher, make_message())

    assert wait_until(lambda: count(db, "SELECT count(*) FROM measurement") == 2)
    assert count(db, "SELECT count(*) FROM rejected_message") == 1
