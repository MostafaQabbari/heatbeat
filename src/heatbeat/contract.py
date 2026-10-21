"""The MQTT message contract: what a gateway is allowed to send us.

What this file does: defines the shape of a telemetry message (Pydantic models)
and the topic names, so every part of the system agrees on one format.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

TOPIC_PREFIX = "hb/v1"
TELEMETRY_SUBSCRIPTION = f"{TOPIC_PREFIX}/+/plants/+/telemetry"

# Lower-case letters, digits and dashes only: no MQTT wildcards (+, #) or slashes.
ID_PATTERN = r"^[a-z0-9-]{1,32}$"


class Reading(BaseModel):
    """One measured value from one sensor at one moment."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    dp: str = Field(min_length=1, max_length=64)  # datapoint, e.g. "flow_temp"
    t: AwareDatetime  # device time, with timezone
    v: float = Field(allow_inf_nan=False)  # the measured value
    q: int = Field(default=0, ge=0)  # quality: 0 = good


class TelemetryMessage(BaseModel):
    """One message from a gateway: a batch of readings for one plant."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = Field(alias="schema", ge=1)
    gateway: str = Field(pattern=ID_PATTERN)
    boot_id: str = Field(min_length=1, max_length=64)
    seq: int = Field(ge=0)
    sent_at: AwareDatetime
    plant: str = Field(pattern=ID_PATTERN)
    readings: list[Reading] = Field(min_length=1, max_length=1000)


class CommandMessage(BaseModel):
    """Cloud -> gateway: change one parameter. Never retained; has an expiry."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = Field(default=1, alias="schema", ge=1)
    command_id: str = Field(min_length=1, max_length=64)
    plant: str = Field(pattern=ID_PATTERN)
    dp: str = Field(min_length=1, max_length=64)
    value: float = Field(allow_inf_nan=False)
    expires_at: AwareDatetime


class CommandResult(BaseModel):
    """Gateway -> cloud: what happened to a command."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = Field(default=1, alias="schema", ge=1)
    command_id: str = Field(min_length=1, max_length=64)
    gateway: str = Field(pattern=ID_PATTERN)
    status: Literal["applied", "rejected", "expired"]
    applied_value: float | None = None
    reason: str | None = None
    at: AwareDatetime


COMMAND_RESULT_SUBSCRIPTION = f"{TOPIC_PREFIX}/+/command-results"


def command_topic(gateway: str) -> str:
    return f"{TOPIC_PREFIX}/{gateway}/commands"


def command_result_topic(gateway: str) -> str:
    return f"{TOPIC_PREFIX}/{gateway}/command-results"


def gateway_from_topic(topic: str, suffix: str) -> str | None:
    """Return the gateway id from 'hb/v1/{gateway}/{suffix}', or None."""
    parts = topic.split("/")
    if len(parts) == 4 and parts[0:2] == ["hb", "v1"] and parts[3] == suffix:
        return parts[2]
    return None


def telemetry_topic(gateway: str, plant: str) -> str:
    """The topic a gateway publishes readings for one plant to."""
    return f"{TOPIC_PREFIX}/{gateway}/plants/{plant}/telemetry"


def parse_telemetry_topic(topic: str) -> tuple[str, str] | None:
    """Return (gateway, plant) from a telemetry topic, or None if it does not match."""
    parts = topic.split("/")
    if len(parts) != 6 or parts[0:2] != ["hb", "v1"]:
        return None
    if parts[3] != "plants" or parts[5] != "telemetry":
        return None
    return parts[2], parts[4]
