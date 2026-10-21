"""API request and response shapes.

What this file does: Pydantic models for everything the API accepts and
returns. FastAPI turns them into the OpenAPI document, so the documentation
cannot drift from the code.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Problem(BaseModel):
    """Error body following RFC 9457 (application/problem+json)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None


# --- auth -------------------------------------------------------------------


class Token(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 (not a password)
    expires_in: int  # seconds


class Me(BaseModel):
    id: int
    email: str
    role: str
    tenant_id: int


# --- plants -----------------------------------------------------------------


class Bucket(StrEnum):
    """Allowed time-bucket sizes for series queries."""

    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    HOUR_1 = "1h"
    DAY_1 = "1d"

    @property
    def interval(self) -> timedelta:
        return {
            "1m": timedelta(minutes=1),
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "1d": timedelta(days=1),
        }[self.value]

    @property
    def uses_hourly_aggregate(self) -> bool:
        return self.interval >= timedelta(hours=1)


class PlantStatus(StrEnum):
    ONLINE = "online"  # data in the last 5 minutes
    OFFLINE = "offline"
    NEVER = "never"  # provisioned, but no data yet


class Site(BaseModel):
    name: str
    address: str | None
    latitude: float | None
    longitude: float | None


class PlantSummary(BaseModel):
    key: str
    name: str | None
    plant_type: str
    gateway: str
    status: PlantStatus
    created_at: datetime
    last_seen_at: datetime | None
    site: Site | None
    open_alarms: int = 0


class PlantPage(BaseModel):
    items: list[PlantSummary]
    next_cursor: str | None  # pass as ?cursor= to get the next page


class LatestValue(BaseModel):
    datapoint: str
    unit: str
    ts: datetime
    value: float
    quality: int


class Parameter(BaseModel):
    datapoint: str
    unit: str
    value: float | None  # last confirmed value (None until the gateway reports or applies one)
    min_value: float
    max_value: float
    version: int  # send as expected_version when changing it (optimistic concurrency)


class PlantDetail(PlantSummary):
    latest: list[LatestValue]
    parameters: list[Parameter] = []


class SeriesPoint(BaseModel):
    ts: datetime
    avg: float
    min: float
    max: float
    count: int


class Series(BaseModel):
    datapoint: str
    unit: str
    bucket: Bucket
    points: list[SeriesPoint]


class ComparePoint(SeriesPoint):
    offset_s: int  # seconds since the start of its period, so both periods can be overlaid


class Comparison(BaseModel):
    datapoint: str
    unit: str
    bucket: Bucket
    period_a: list[ComparePoint]
    period_b: list[ComparePoint]
    mean_a: float | None
    mean_b: float | None
    change_pct: float | None = Field(description="(mean_b - mean_a) / |mean_a| * 100")


# --- alarms -----------------------------------------------------------------

RuleKindName = Literal["threshold", "spread", "rate", "flatline", "absence"]
UrgencyName = Literal["low", "medium", "high", "critical"]
PlantTypeName = Literal["gas_boiler", "heat_pump", "district_heating"]


class AlarmRule(BaseModel):
    id: int
    name: str
    kind: RuleKindName
    datapoint: str | None
    second_datapoint: str | None
    plant_type: str | None
    above: bool
    threshold: float
    delay_s: int
    hysteresis: float
    window_s: int | None
    urgency: UrgencyName
    enabled: bool


class AlarmRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: RuleKindName
    datapoint: str | None = None
    second_datapoint: str | None = None
    plant_type: PlantTypeName | None = None
    above: bool = True
    threshold: float = Field(allow_inf_nan=False)
    delay_s: int = Field(default=0, ge=0, le=86_400)
    hysteresis: float = Field(default=0, ge=0, allow_inf_nan=False)
    window_s: int | None = Field(default=None, gt=0, le=86_400)
    urgency: UrgencyName = "medium"

    @model_validator(mode="after")
    def check_kind(self) -> "AlarmRuleCreate":
        if self.kind != "absence" and not self.datapoint:
            raise ValueError("this rule kind needs a datapoint")
        if self.kind == "spread" and not self.second_datapoint:
            raise ValueError("a spread rule needs second_datapoint")
        return self


class AlarmRuleUpdate(BaseModel):
    enabled: bool | None = None
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    delay_s: int | None = Field(default=None, ge=0, le=86_400)
    hysteresis: float | None = Field(default=None, ge=0)
    urgency: UrgencyName | None = None


class Alarm(BaseModel):
    id: int
    plant_key: str
    plant_name: str | None
    rule_id: int
    rule_name: str
    kind: str
    urgency: str
    state: Literal["normal", "pending", "firing"]
    since: datetime | None
    opened_at: datetime | None
    last_value: float | None
    threshold: float
    acknowledged_by: str | None
    acknowledged_at: datetime | None


class AlarmEvent(BaseModel):
    id: int
    plant_key: str
    rule_name: str
    urgency: str
    kind: Literal["fired", "cleared"]
    at: datetime
    value: float | None


class FaultStat(BaseModel):
    rule_name: str
    urgency: str
    occurrences: int
    open_now: int
    mean_duration_s: float | None  # of the occurrences that have ended
    total_duration_s: float


class PlantFaults(BaseModel):
    plant_key: str
    occurrences: int
    total_duration_s: float


class FaultStats(BaseModel):
    start: datetime
    end: datetime
    total_occurrences: int
    most_frequent: str | None
    by_rule: list[FaultStat]
    by_plant: list[PlantFaults]


# --- commands ---------------------------------------------------------------


class CommandCreate(BaseModel):
    datapoint: str
    value: float = Field(allow_inf_nan=False)
    expected_version: int = Field(ge=0, description="The parameter version you saw (from GET /v1/plants/{key})")


class Command(BaseModel):
    id: str
    plant_key: str
    datapoint: str
    requested_value: float
    previous_value: float | None
    state: str
    needs_approval: bool
    source: str
    requested_by: str | None
    approved_by: str | None
    reason: str | None
    applied_value: float | None
    created_at: datetime
    sent_at: datetime | None
    expires_at: datetime
    result_at: datetime | None


class AuditEntry(BaseModel):
    id: int
    at: datetime
    datapoint: str
    old_value: float | None
    new_value: float
    source: str
    actor: str | None
    approved_by: str | None
    command_id: str | None


# --- recommendations --------------------------------------------------------


class Recommendation(BaseModel):
    id: int
    plant_key: str
    kind: str
    title: str
    rationale: str
    datapoint: str
    current_value: float | None
    proposed_value: float
    state: str
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    command_id: str | None
    measured_effect: str | None
