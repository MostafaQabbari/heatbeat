"""Plant endpoints: portfolio, detail, time series, period comparison.

What this file does: checks every request before the database is asked
(limits on time range and number of points), then calls the repository.
A plant of another tenant is simply "not found" (uniform 404), so the API
never confirms that it exists.
"""

import base64
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from heatbeat.api.deps import TenantDb, rate_limited
from heatbeat.api.problems import ApiProblem
from heatbeat.api.repository import PlantRepository, PostgresRepository
from heatbeat.api.schemas import (
    Bucket,
    ComparePoint,
    Comparison,
    PlantDetail,
    PlantPage,
    PlantStatus,
    Problem,
    Series,
)

MAX_RANGE = timedelta(days=31)
MAX_POINTS = 5000
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": Problem, "description": "Plant not found"}}
OPEN_ALARMS_SQL = "(SELECT count(*) FROM alarm a WHERE a.plant_id = p.id AND a.state = 'firing')"


def get_repository(conn: TenantDb) -> PlantRepository:
    return PostgresRepository(conn, open_alarms_sql=OPEN_ALARMS_SQL)


Repo = Annotated[PlantRepository, Depends(get_repository)]
router = APIRouter(prefix="/v1", tags=["plants"], dependencies=[Depends(rate_limited)])


def encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(f"id:{last_id}".encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        prefix, value = base64.urlsafe_b64decode(cursor.encode()).decode().split(":")
        if prefix != "id":
            raise ValueError
        return int(value)
    except ValueError as exc:
        raise ApiProblem(400, "Invalid cursor") from exc


def parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        min_lon, min_lat, max_lon, max_lat = (float(x) for x in bbox.split(","))
    except ValueError as exc:
        raise ApiProblem(422, "Invalid bbox", "Use min_lon,min_lat,max_lon,max_lat") from exc
    if min_lon > max_lon or min_lat > max_lat:
        raise ApiProblem(422, "Invalid bbox", "min must be smaller than max")
    return min_lon, min_lat, max_lon, max_lat


def check_range(start: AwareDatetime, end: AwareDatetime, bucket: Bucket) -> None:
    if end <= start:
        raise ApiProblem(422, "Invalid time range", "'to' must be after 'from'")
    if end - start > MAX_RANGE:
        raise ApiProblem(422, "Time range too long", f"Maximum is {MAX_RANGE.days} days")
    if (end - start) / bucket.interval > MAX_POINTS:
        raise ApiProblem(422, "Too many points", f"Use a larger bucket (max {MAX_POINTS} points)")


def check_datapoints(repo: PlantRepository, datapoints: list[str]) -> None:
    unknown = set(datapoints) - repo.datapoint_keys()
    if unknown:
        raise ApiProblem(422, "Unknown datapoint", ", ".join(sorted(unknown)))


def require_plant(repo: PlantRepository, key: str) -> None:
    if repo.get_plant(key) is None:
        raise ApiProblem(404, "Plant not found", f"No plant with key {key!r}")


@router.get("/plants", response_model=PlantPage)
def list_plants(
    repo: Repo,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    plant_type: Annotated[str | None, Query(alias="type")] = None,
    status: PlantStatus | None = None,
    bbox: Annotated[str | None, Query(description="min_lon,min_lat,max_lon,max_lat")] = None,
) -> PlantPage:
    after_id = decode_cursor(cursor) if cursor else 0
    box = parse_bbox(bbox) if bbox else None
    rows = repo.list_plants(limit + 1, after_id, plant_type, status, box)  # one extra: is there more?
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1][0]) if len(rows) > limit else None
    return PlantPage(items=[plant for _, plant in page], next_cursor=next_cursor)


@router.get("/plants/{plant_key}", response_model=PlantDetail, responses=NOT_FOUND)
def get_plant(plant_key: str, repo: Repo) -> PlantDetail:
    plant = repo.get_plant(plant_key)
    if plant is None:
        raise ApiProblem(404, "Plant not found", f"No plant with key {plant_key!r}")
    return PlantDetail(
        **plant.model_dump(), latest=repo.latest_values(plant_key), parameters=repo.parameters(plant_key)
    )


@router.get("/plants/{plant_key}/series", response_model=list[Series], responses=NOT_FOUND)
def get_series(
    plant_key: str,
    repo: Repo,
    dp: Annotated[list[str], Query(min_length=1, max_length=10, description="Datapoint keys")],
    start: Annotated[AwareDatetime, Query(alias="from")],
    end: Annotated[AwareDatetime, Query(alias="to")],
    bucket: Bucket = Bucket.MIN_5,
) -> list[Series]:
    """Several datapoints in one call. Buckets of 1h or more read the hourly aggregate."""
    check_range(start, end, bucket)
    check_datapoints(repo, dp)
    require_plant(repo, plant_key)
    return repo.series(plant_key, dp, start, end, bucket)


@router.get("/plants/{plant_key}/compare", response_model=Comparison, responses=NOT_FOUND)
def compare_periods(
    plant_key: str,
    repo: Repo,
    dp: str,
    a_from: AwareDatetime,
    a_to: AwareDatetime,
    b_from: AwareDatetime,
    b_to: AwareDatetime,
    bucket: Bucket = Bucket.HOUR_1,
) -> Comparison:
    """Compare one datapoint over two periods (for example, this week vs. last week)."""
    check_range(a_from, a_to, bucket)
    check_range(b_from, b_to, bucket)
    check_datapoints(repo, [dp])
    require_plant(repo, plant_key)

    def period(start: AwareDatetime, end: AwareDatetime) -> tuple[list[ComparePoint], str, float | None]:
        found = repo.series(plant_key, [dp], start, end, bucket)
        if not found:
            return [], "", None
        points = [ComparePoint(**p.model_dump(), offset_s=int((p.ts - start).total_seconds())) for p in found[0].points]
        total = sum(p.count for p in points)
        mean = sum(p.avg * p.count for p in points) / total if total else None
        return points, found[0].unit, mean

    a_points, unit_a, mean_a = period(a_from, a_to)
    b_points, unit_b, mean_b = period(b_from, b_to)
    change = None
    if mean_a not in (None, 0) and mean_b is not None:
        change = round((mean_b - mean_a) / abs(mean_a) * 100, 2)
    return Comparison(
        datapoint=dp,
        unit=unit_a or unit_b,
        bucket=bucket,
        period_a=a_points,
        period_b=b_points,
        mean_a=mean_a,
        mean_b=mean_b,
        change_pct=change,
    )
