"""HTTP routes, version 1.

What this file does: the /v1 endpoints. It checks the request (limits on time
range and number of points protect the database), asks the repository for data
and returns typed responses. No SQL here.
"""

import base64
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime

from heatbeat.api.repository import PlantRepository, PostgresRepository
from heatbeat.api.schemas import Bucket, PlantDetail, PlantPage, Problem, Series

MAX_RANGE = timedelta(days=31)
MAX_POINTS = 5000
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": Problem, "description": "Plant not found"}}


class ApiProblem(Exception):
    """Raised by routes; turned into an application/problem+json response."""

    def __init__(self, status: int, title: str, detail: str | None = None) -> None:
        self.problem = Problem(status=status, title=title, detail=detail)


def get_repository(request: Request) -> PlantRepository:
    return PostgresRepository(request.app.state.pool)


Repo = Annotated[PlantRepository, Depends(get_repository)]
router = APIRouter(prefix="/v1", tags=["plants"])


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


@router.get("/plants", response_model=PlantPage)
def list_plants(
    repo: Repo,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> PlantPage:
    after_id = decode_cursor(cursor) if cursor else 0
    rows = repo.list_plants(limit=limit + 1, after_id=after_id)  # one extra: is there more?
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1][0]) if len(rows) > limit else None
    return PlantPage(items=[plant for _, plant in page], next_cursor=next_cursor)


@router.get("/plants/{plant_key}", response_model=PlantDetail, responses=NOT_FOUND)
def get_plant(plant_key: str, repo: Repo) -> PlantDetail:
    plant = repo.get_plant(plant_key)
    if plant is None:
        raise ApiProblem(404, "Plant not found", f"No plant with key {plant_key!r}")
    return PlantDetail(**plant.model_dump(), latest=repo.latest_values(plant_key))


@router.get("/plants/{plant_key}/series", response_model=list[Series], responses=NOT_FOUND)
def get_series(
    plant_key: str,
    repo: Repo,
    dp: Annotated[list[str], Query(min_length=1, max_length=10, description="Datapoint keys")],
    start: Annotated[AwareDatetime, Query(alias="from")],
    end: Annotated[AwareDatetime, Query(alias="to")],
    bucket: Bucket = Bucket.MIN_5,
) -> list[Series]:
    if end <= start:
        raise ApiProblem(422, "Invalid time range", "'to' must be after 'from'")
    if end - start > MAX_RANGE:
        raise ApiProblem(422, "Time range too long", f"Maximum is {MAX_RANGE.days} days")
    if (end - start) / bucket.interval > MAX_POINTS:
        raise ApiProblem(422, "Too many points", f"Use a larger bucket (max {MAX_POINTS} points)")
    unknown = set(dp) - repo.datapoint_keys()
    if unknown:
        raise ApiProblem(422, "Unknown datapoint", ", ".join(sorted(unknown)))
    if repo.get_plant(plant_key) is None:
        raise ApiProblem(404, "Plant not found", f"No plant with key {plant_key!r}")
    return repo.series(plant_key, dp, start, end, bucket)
