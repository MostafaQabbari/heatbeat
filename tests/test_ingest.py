"""Tests for the ingest rules (src/heatbeat/ingest.py): pure functions, no database."""

from datetime import UTC, datetime, timedelta

import pytest
from helpers import NOW, as_bytes, make_message, topic_for

from heatbeat.contract import Reading, TelemetryMessage
from heatbeat.ingest import (
    QUALITY_IMPLAUSIBLE,
    PlausibleRange,
    Rejection,
    SeqStatus,
    SequenceTracker,
    check_reading,
    decode,
)

CATALOGUE = {"flow_temp": PlausibleRange(0, 110), "return_temp": PlausibleRange(0, 110)}


def reading(t: datetime = NOW, v: float = 50.0, dp: str = "flow_temp") -> Reading:
    return Reading(dp=dp, t=t, v=v)


# --- decode -----------------------------------------------------------------


def test_valid_message_is_decoded():
    msg = make_message()

    result = decode(topic_for(msg), as_bytes(msg))

    assert isinstance(result, TelemetryMessage)
    assert result.plant == "p-17"


def test_broken_json_becomes_rejection_not_crash():
    result = decode("hb/v1/gw-1/plants/p-1/telemetry", b"{not json")

    assert isinstance(result, Rejection)
    assert result.reason.startswith("invalid message")


def test_unknown_topic_is_rejected():
    result = decode("somewhere/else", as_bytes(make_message()))

    assert isinstance(result, Rejection)
    assert result.reason == "unknown topic"


def test_gateway_cannot_speak_for_another_gateway():
    """Security: the payload must match the topic the gateway is allowed to publish to."""
    msg = make_message(gateway="gw-0042")

    result = decode("hb/v1/gw-9999/plants/p-17/telemetry", as_bytes(msg))

    assert isinstance(result, Rejection)
    assert "does not match" in result.reason


# --- check_reading ----------------------------------------------------------


def test_good_reading_keeps_its_quality():
    assert check_reading(reading(), NOW, CATALOGUE) == 0


def test_unknown_datapoint_is_rejected():
    assert "unknown datapoint" in str(check_reading(reading(dp="mystery"), NOW, CATALOGUE))


def test_implausible_value_is_kept_but_flagged():
    """A 500 degC flow temperature is stored for diagnosis, but marked implausible."""
    assert check_reading(reading(v=500), NOW, CATALOGUE) == QUALITY_IMPLAUSIBLE


def test_s06_reading_from_the_future_is_rejected():
    """S06: a device clock far ahead must not create readings 'in the future'."""
    future = NOW + timedelta(hours=1)

    assert check_reading(reading(t=future), NOW, CATALOGUE) == "timestamp in the future"


def test_s06_small_clock_skew_is_tolerated():
    assert check_reading(reading(t=NOW + timedelta(minutes=2)), NOW, CATALOGUE) == 0


def test_s06_reading_from_1970_is_rejected():
    """S06: a device that lost its clock reports 1970."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    assert "too old" in str(check_reading(reading(t=epoch), NOW, CATALOGUE))


# --- SequenceTracker (S03, S07) ---------------------------------------------


@pytest.fixture
def tracker() -> SequenceTracker:
    t = SequenceTracker()
    t.observe("gw-1", "boot-a", 10)
    return t


def test_first_message_from_gateway():
    assert SequenceTracker().observe("gw-1", "boot-a", 10) is SeqStatus.FIRST


def test_next_sequence_is_ok(tracker):
    assert tracker.observe("gw-1", "boot-a", 11) is SeqStatus.OK


def test_s01_redelivered_message_is_seen_as_duplicate(tracker):
    assert tracker.observe("gw-1", "boot-a", 10) is SeqStatus.DUPLICATE


def test_s03_missing_messages_are_detected_as_gap(tracker):
    assert tracker.observe("gw-1", "boot-a", 15) is SeqStatus.GAP


def test_s07_reboot_resets_seq_but_is_recognised_by_boot_id(tracker):
    """S07: after a reboot seq starts at 1 again; the new boot_id tells us why."""
    assert tracker.observe("gw-1", "boot-b", 1) is SeqStatus.RESTART
    assert tracker.observe("gw-1", "boot-b", 2) is SeqStatus.OK


def test_gateways_are_tracked_independently(tracker):
    assert tracker.observe("gw-2", "boot-x", 500) is SeqStatus.FIRST
    assert tracker.observe("gw-1", "boot-a", 11) is SeqStatus.OK
