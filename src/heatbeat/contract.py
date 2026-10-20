"""The MQTT message contract: what a gateway is allowed to send us."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Reading(BaseModel):
    """One measured value from one sensor at one moment."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    dp: str = Field(min_length=1, max_length=64)  # datapoint, e.g. "flow_temp"
    t: AwareDatetime                              # device time, with timezone
    v: float = Field(allow_inf_nan=False)         # the measured value
    q: int = Field(default=0, ge=0)               # quality: 0 = good


class TelemetryMessage(BaseModel):
    """One message from a gateway: a batch of readings for one plant."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = Field(alias="schema", ge=1)
    gateway: str
    boot_id: str
    seq: int = Field(ge=0)
    sent_at: AwareDatetime
    plant: str
    readings: list[Reading] = Field(min_length=1, max_length=1000)