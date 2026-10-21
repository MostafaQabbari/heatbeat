"""Tests for the MQTT message contract (src/heatbeat/contract.py)."""

import pytest
from pydantic import ValidationError

from heatbeat.contract import TelemetryMessage, parse_telemetry_topic, telemetry_topic


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
        ("seq", -1),  # sequence numbers never go negative
        ("readings", []),  # a message must carry at least one reading
        ("schema", 0),  # versions start at 1
        ("gateway", "gw/+/#"),  # MQTT wildcards and slashes are not allowed in ids
    ],
)
def test_invalid_envelope_is_rejected(field, bad_value):
    data = valid_message()
    data[field] = bad_value

    with pytest.raises(ValidationError):
        TelemetryMessage.model_validate(data)


def test_topic_round_trip():
    topic = telemetry_topic("gw-0042", "p-17")

    assert topic == "hb/v1/gw-0042/plants/p-17/telemetry"
    assert parse_telemetry_topic(topic) == ("gw-0042", "p-17")


@pytest.mark.parametrize(
    "topic",
    ["hb/v2/gw/plants/p/telemetry", "hb/v1/gw/plants/p", "hb/v1/gw/devices/p/telemetry", "other"],
)
def test_foreign_topics_are_not_parsed(topic):
    assert parse_telemetry_topic(topic) is None


def test_command_message_round_trip():
    from heatbeat.contract import CommandMessage, command_topic, gateway_from_topic

    msg = CommandMessage.model_validate(
        {
            "command_id": "c-1",
            "plant": "p-17",
            "dp": "heating_curve_level",
            "value": 24,
            "expires_at": "2026-10-21T12:00:00Z",
        }
    )

    assert command_topic("gw-0042") == "hb/v1/gw-0042/commands"
    assert gateway_from_topic("hb/v1/gw-0042/commands", "commands") == "gw-0042"
    assert msg.schema_version == 1


def test_command_result_status_is_a_closed_list():
    from heatbeat.contract import CommandResult

    with pytest.raises(ValidationError):
        CommandResult.model_validate(
            {"command_id": "c", "gateway": "gw-1", "status": "maybe", "at": "2026-10-21T12:00:00Z"}
        )
