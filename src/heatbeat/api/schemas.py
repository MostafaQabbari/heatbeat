"""API response and error shapes.

What this file does: Pydantic models for everything the API returns. FastAPI
turns them into the OpenAPI document, so the docs can never drift from the code.
"""

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel


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


class PlantSummary(BaseModel):
    key: str
    gateway: str
    created_at: datetime
    last_seen_at: datetime | None


class PlantPage(BaseModel):
    items: list[PlantSummary]
    next_cursor: str | None  # pass as ?cursor= to get the next page


class LatestValue(BaseModel):
    datapoint: str
    unit: str
    ts: datetime
    value: float
    quality: int


class PlantDetail(PlantSummary):
    latest: list[LatestValue]


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


class Problem(BaseModel):
    """Error body following RFC 9457 (application/problem+json)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
