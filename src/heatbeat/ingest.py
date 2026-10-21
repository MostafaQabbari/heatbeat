"""Ingest rules: decide what to do with each incoming message.

What this file does: pure functions with no network and no database. It turns
raw MQTT bytes into checked messages or a rejection reason, flags implausible
values, refuses impossible timestamps, and detects sequence gaps, duplicates
and gateway restarts. Because it is pure, every rule is easy to unit-test.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import ValidationError

from heatbeat.contract import Reading, TelemetryMessage, parse_telemetry_topic

QUALITY_GOOD = 0
QUALITY_IMPLAUSIBLE = 2

MAX_FUTURE_SKEW = timedelta(minutes=5)  # device clock may run a little fast
OLDEST_ACCEPTED = datetime(2020, 1, 1, tzinfo=UTC)  # catches 1970 clocks


@dataclass(frozen=True)
class Rejection:
    """A message or reading we refuse, with the reason we store for a person."""

    topic: str
    reason: str
    payload: str


@dataclass(frozen=True)
class PlausibleRange:
    low: float
    high: float


def decode(topic: str, payload: bytes) -> TelemetryMessage | Rejection:
    """Validate one MQTT message. Never raises: bad input becomes a Rejection."""
    text = payload.decode("utf-8", errors="replace")
    ids = parse_telemetry_topic(topic)
    if ids is None:
        return Rejection(topic, "unknown topic", text)
    try:
        msg = TelemetryMessage.model_validate_json(payload)
    except ValidationError as exc:
        return Rejection(topic, f"invalid message: {exc.errors()[0]['msg']}", text)

    # A gateway may only speak for itself and its own plant.
    if (msg.gateway, msg.plant) != ids:
        return Rejection(topic, "topic does not match gateway/plant in payload", text)
    return msg


def check_reading(
    reading: Reading,
    now: datetime,
    catalogue: dict[str, PlausibleRange],
) -> int | str:
    """Return the quality code to store, or a rejection reason (str)."""
    if reading.dp not in catalogue:
        return f"unknown datapoint {reading.dp!r}"
    if reading.t > now + MAX_FUTURE_SKEW:
        return "timestamp in the future"
    if reading.t < OLDEST_ACCEPTED:
        return "timestamp too old (device clock not set?)"
    limits = catalogue[reading.dp]
    if not limits.low <= reading.v <= limits.high:
        return QUALITY_IMPLAUSIBLE  # keep the raw value, but mark it
    return reading.q


class SeqStatus(StrEnum):
    FIRST = "first"
    OK = "ok"
    DUPLICATE = "duplicate"
    GAP = "gap"
    RESTART = "restart"


class SequenceTracker:
    """Remembers the last (boot_id, seq) per gateway to spot gaps and restarts.

    Kept in memory: after a bridge restart the first message per gateway is
    simply FIRST again. Detection is for monitoring; storage stays idempotent.
    """

    def __init__(self) -> None:
        self._last: dict[str, tuple[str, int]] = {}

    def observe(self, gateway: str, boot_id: str, seq: int) -> SeqStatus:
        previous = self._last.get(gateway)
        if previous is None:
            self._last[gateway] = (boot_id, seq)
            return SeqStatus.FIRST
        last_boot, last_seq = previous
        if boot_id != last_boot:
            self._last[gateway] = (boot_id, seq)
            return SeqStatus.RESTART
        if seq <= last_seq:
            return SeqStatus.DUPLICATE
        self._last[gateway] = (boot_id, seq)
        return SeqStatus.OK if seq == last_seq + 1 else SeqStatus.GAP
