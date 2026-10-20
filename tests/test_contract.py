"""Tests for the MQTT message contract (src/heatbeat/contract.py)."""

import pytest
from pydantic import ValidationError

from heatbeat.contract import TelemetryMessage


def valid_message() -> dict:
    """A correct message. Each test breaks exactly one thing in it."""
    return {
        "schema": 1,
        "gateway": "gw-0042",
        "boot_id": "7f0c",
        "seq": 18211,
        "sent_at": "2026-10-17T14:05:30Z",
        "plant": "p-17",
        "readings": [
            {"dp": "flow_temp", "t": "2026-10-17T14:05:00Z", "v": 62.4},
        ],
    }


def test_valid_message_is_accepted():
    msg = TelemetryMessage.model_validate(valid_message())

    assert msg.gateway == "gw-0042"
    assert msg.readings[0].v == 62.4
    assert msg.readings[0].q == 0  # default quality


def test_s10_unknown_fields_are_ignored():
    """S10: newer firmware adds fields; we must still understand the message."""
    data = valid_message()
    data["firmware"] = "2.0"
    data["readings"][0]["unit"] = "C"

    msg = TelemetryMessage.model_validate(data)

    assert msg.readings[0].v == 62.4


def test_s06_time_without_timezone_is_rejected():
    """S06: a timestamp without a timezone is ambiguous."""
    data = valid_message()
    data["readings"][0]["t"] = "2026-10-17T14:05:00"

    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(data)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_broken_sensor_values_are_rejected(bad_value):
    data = valid_message()
    data["readings"][0]["v"] = bad_value

    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(data)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("seq", -1),        # sequence numbers never go negative
        ("readings", []),   # a message must carry at least one reading
        ("schema", 0),      # versions start at 1
    ],
)
def test_invalid_envelope_is_rejected(field, bad_value):
    data = valid_message()
    data[field] = bad_value

    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(data)