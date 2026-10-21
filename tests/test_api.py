"""Tests for the REST API (src/heatbeat/api/), with a fake repository instead of the database."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from heatbeat.api.main import create_app
from heatbeat.api.routes import get_repository
from heatbeat.api.schemas import LatestValue, PlantSummary, Series, SeriesPoint

T = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)


class FakeRepository:
    def __init__(self, plant_count: int = 3, healthy: bool = True) -> None:
        self.plants = [
            (i, PlantSummary(key=f"p-{i}", gateway=f"gw-{i}", created_at=T, last_seen_at=T))
            for i in range(1, plant_count + 1)
        ]
        self.healthy = healthy
        self.series_calls: list[tuple] = []

    def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("down")
        return True

    def list_plants(self, limit, after_id):
        return [(i, p) for i, p in self.plants if i > after_id][:limit]

    def get_plant(self, key):
        return next((p for _, p in self.plants if p.key == key), None)

    def latest_values(self, key):
        return [LatestValue(datapoint="flow_temp", unit="degC", ts=T, value=60.0, quality=0)]

    def datapoint_keys(self):
        return {"flow_temp", "return_temp"}

    def series(self, key, datapoints, start, end, bucket):
        self.series_calls.append((key, datapoints, start, end, bucket))
        point = SeriesPoint(ts=start, avg=60, min=55, max=65, count=10)
        return [Series(datapoint=dp, unit="degC", bucket=bucket, points=[point]) for dp in datapoints]


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def client(repo) -> TestClient:
    app = create_app(connect_db=False)
    app.dependency_overrides[get_repository] = lambda: repo
    return TestClient(app)


SERIES = "/v1/plants/p-1/series"
RANGE = {"from": "2026-10-17T00:00:00Z", "to": "2026-10-18T00:00:00Z"}


def assert_problem(response, status: int, title: str) -> None:
    """Every error has the same shape: application/problem+json."""
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == title


def test_health_does_not_need_the_database(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_reports_database_down():
    app = create_app(connect_db=False)
    app.dependency_overrides[get_repository] = lambda: FakeRepository(healthy=False)

    assert_problem(TestClient(app).get("/ready"), 503, "Database unavailable")


def test_list_plants_pages_with_a_cursor(client):
    first = client.get("/v1/plants", params={"limit": 2}).json()
    second = client.get("/v1/plants", params={"limit": 2, "cursor": first["next_cursor"]}).json()

    assert [p["key"] for p in first["items"]] == ["p-1", "p-2"]
    assert [p["key"] for p in second["items"]] == ["p-3"]
    assert second["next_cursor"] is None


def test_invalid_cursor_is_a_clear_error(client):
    assert_problem(client.get("/v1/plants", params={"cursor": "abc"}), 400, "Invalid cursor")


def test_limit_is_bounded(client):
    assert_problem(client.get("/v1/plants", params={"limit": 10_000}), 422, "Invalid request")


def test_plant_detail_includes_latest_values(client):
    body = client.get("/v1/plants/p-1").json()

    assert body["key"] == "p-1"
    assert body["latest"][0]["datapoint"] == "flow_temp"


def test_unknown_plant_is_404_problem(client):
    assert_problem(client.get("/v1/plants/nope"), 404, "Plant not found")


def test_series_for_several_datapoints_in_one_call(client, repo):
    response = client.get(SERIES, params={**RANGE, "dp": ["flow_temp", "return_temp"], "bucket": "1h"})

    assert response.status_code == 200
    assert [s["datapoint"] for s in response.json()] == ["flow_temp", "return_temp"]
    assert repo.series_calls[0][4] == "1h"


@pytest.mark.parametrize(
    ("params", "title"),
    [
        ({"from": "2026-10-18T00:00:00Z", "to": "2026-10-17T00:00:00Z"}, "Invalid time range"),
        ({"from": "2026-01-01T00:00:00Z", "to": "2026-10-17T00:00:00Z"}, "Time range too long"),
        ({**RANGE, "bucket": "1m"} | {"to": "2026-10-21T00:00:00Z"}, "Too many points"),
        ({"from": "2026-10-17T00:00:00", "to": "2026-10-18T00:00:00Z"}, "Invalid request"),
        ({**RANGE, "bucket": "7m"}, "Invalid request"),
    ],
)
def test_series_limits_protect_the_database(client, repo, params, title):
    response = client.get(SERIES, params={"dp": "flow_temp", **params})

    assert_problem(response, 422, title)
    assert repo.series_calls == []  # the database was never asked


def test_series_rejects_unknown_datapoint(client):
    assert_problem(client.get(SERIES, params={**RANGE, "dp": "mystery"}), 422, "Unknown datapoint")


def test_series_for_unknown_plant_is_404(client):
    response = client.get("/v1/plants/nope/series", params={**RANGE, "dp": "flow_temp"})

    assert_problem(response, 404, "Plant not found")


def test_all_routes_are_versioned(client):
    paths = client.get("/openapi.json").json()["paths"]

    business = [p for p in paths if p not in ("/health", "/ready")]
    assert business
    assert all(p.startswith("/v1/") for p in business)
