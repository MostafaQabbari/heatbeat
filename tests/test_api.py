"""Tests for the REST API (src/heatbeat/api/) with fake repositories (no database).

Tokens are real signed JWTs, so authentication is tested for real. Tenant
isolation in the database is tested in test_integration_security.py.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from heatbeat.api.main import create_app
from heatbeat.api.routes import auth as auth_routes
from heatbeat.api.routes import plants as plant_routes
from heatbeat.api.schemas import LatestValue, PlantStatus, PlantSummary, Series, SeriesPoint
from heatbeat.auth import Role, User, create_token, hash_password
from heatbeat.config import Settings

T = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)
SECRET = "unit-test-secret-0123456789abcdef-0123"
OPERATOR = User(id=1, tenant_id=10, email="op@a.example", role=Role.OPERATOR)
OTHER_TENANT = User(id=2, tenant_id=20, email="op@b.example", role=Role.OPERATOR)


class FakeRepository:
    def __init__(self, plant_count: int = 3) -> None:
        self.plants = [
            (
                i,
                PlantSummary(
                    key=f"p-{i}",
                    name=None,
                    plant_type="gas_boiler",
                    gateway=f"gw-{i}",
                    status=PlantStatus.ONLINE,
                    created_at=T,
                    last_seen_at=T,
                    site=None,
                ),
            )
            for i in range(1, plant_count + 1)
        ]
        self.series_calls: list[tuple] = []

    def ping(self) -> bool:
        return True

    def list_plants(self, limit, after_id, plant_type=None, status=None, bbox=None):
        return [(i, p) for i, p in self.plants if i > after_id][:limit]

    def get_plant(self, key):
        return next((p for _, p in self.plants if p.key == key), None)

    def latest_values(self, key):
        return [LatestValue(datapoint="flow_temp", unit="degC", ts=T, value=60.0, quality=0)]

    def parameters(self, key):
        return []

    def datapoint_keys(self):
        return {"flow_temp", "return_temp"}

    def series(self, key, datapoints, start, end, bucket):
        self.series_calls.append((key, datapoints, start, end, bucket))
        point = SeriesPoint(ts=start, avg=60, min=55, max=65, count=10)
        return [Series(datapoint=dp, unit="degC", bucket=bucket, points=[point]) for dp in datapoints]


class FakeLogins:
    def __init__(self) -> None:
        self.users = {"op@a.example": (OPERATOR, hash_password("right-password"))}

    def find(self, email):
        return self.users.get(email)


def settings(**extra) -> Settings:
    return Settings(_env_file=None, db_password="unused", jwt_secret=SECRET, **extra)  # type: ignore[arg-type]


def make_client(repo: FakeRepository, **extra) -> TestClient:
    app = create_app(settings(**extra), connect_db=False)
    app.dependency_overrides[plant_routes.get_repository] = lambda: repo
    app.dependency_overrides[auth_routes.get_login_repository] = FakeLogins
    return TestClient(app)


def bearer(user: User = OPERATOR, ttl: timedelta = timedelta(minutes=5)) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(user, SECRET, ttl)}"}


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def client(repo) -> TestClient:
    return make_client(repo)


SERIES = "/v1/plants/p-1/series"
RANGE = {"from": "2026-10-17T00:00:00Z", "to": "2026-10-18T00:00:00Z"}


def assert_problem(response, status: int, title: str) -> None:
    """Every error has the same shape: application/problem+json."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == title


# --- start-up and auth ------------------------------------------------------------


def test_app_refuses_to_start_without_a_strong_jwt_secret():
    with pytest.raises(ValueError, match="HB_JWT_SECRET"):
        create_app(Settings(_env_file=None, db_password="x", jwt_secret="short"), connect_db=False)  # type: ignore[arg-type]


def test_health_needs_no_login(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_no_token_is_401_with_challenge(client):
    response = client.get("/v1/plants")

    assert_problem(response, 401, "Not authenticated")
    assert response.headers["www-authenticate"] == "Bearer"


def test_expired_token_is_401(client):
    assert_problem(client.get("/v1/plants", headers=bearer(ttl=timedelta(seconds=-1))), 401, "Invalid or expired token")


def test_login_returns_a_working_token(client):
    response = client.post("/v1/auth/token", data={"username": "op@a.example", "password": "right-password"})
    token = response.json()["access_token"]

    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()

    assert me == {"id": 1, "email": "op@a.example", "role": "operator", "tenant_id": 10}


@pytest.mark.parametrize(("email", "password"), [("op@a.example", "wrong"), ("nobody@a.example", "x")])
def test_wrong_password_and_unknown_user_look_the_same(client, email, password):
    """Nobody can find out which e-mail addresses have an account."""
    response = client.post("/v1/auth/token", data={"username": email, "password": password})

    assert_problem(response, 401, "Wrong e-mail or password")


def test_s17_one_tenant_is_rate_limited_others_are_not(repo):
    client = make_client(repo, rate_limit_per_minute=3)
    for _ in range(3):
        assert client.get("/v1/plants", headers=bearer(OPERATOR)).status_code == 200

    blocked = client.get("/v1/plants", headers=bearer(OPERATOR))
    other = client.get("/v1/plants", headers=bearer(OTHER_TENANT))

    assert_problem(blocked, 429, "Too many requests")
    assert int(blocked.headers["retry-after"]) >= 1
    assert other.status_code == 200


def test_cors_allows_the_portal_origin(client):
    response = client.options(
        "/v1/plants",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_every_response_has_a_request_id(client):
    assert client.get("/health").headers["x-request-id"]
    assert client.get("/health", headers={"X-Request-ID": "abc"}).headers["x-request-id"] == "abc"


def test_metrics_endpoint_speaks_prometheus(client):
    client.get("/health")

    body = client.get("/metrics").text

    assert "heatbeat_http_requests_total" in body


# --- plants -----------------------------------------------------------------------


def test_list_plants_pages_with_a_cursor(client):
    first = client.get("/v1/plants", params={"limit": 2}, headers=bearer()).json()
    second = client.get("/v1/plants", params={"limit": 2, "cursor": first["next_cursor"]}, headers=bearer()).json()

    assert [p["key"] for p in first["items"]] == ["p-1", "p-2"]
    assert [p["key"] for p in second["items"]] == ["p-3"]
    assert second["next_cursor"] is None


def test_invalid_cursor_is_a_clear_error(client):
    assert_problem(client.get("/v1/plants", params={"cursor": "abc"}, headers=bearer()), 400, "Invalid cursor")


@pytest.mark.parametrize("bbox", ["1,2,3", "9,50,8,51", "a,b,c,d"])
def test_bad_bbox_is_rejected(client, bbox):
    assert_problem(client.get("/v1/plants", params={"bbox": bbox}, headers=bearer()), 422, "Invalid bbox")


def test_plant_detail_includes_latest_values(client):
    body = client.get("/v1/plants/p-1", headers=bearer()).json()

    assert body["latest"][0]["datapoint"] == "flow_temp"


def test_unknown_plant_is_404_problem(client):
    assert_problem(client.get("/v1/plants/nope", headers=bearer()), 404, "Plant not found")


def test_series_for_several_datapoints_in_one_call(client, repo):
    params = {**RANGE, "dp": ["flow_temp", "return_temp"], "bucket": "1h"}

    response = client.get(SERIES, params=params, headers=bearer())

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
    response = client.get(SERIES, params={"dp": "flow_temp", **params}, headers=bearer())

    assert_problem(response, 422, title)
    assert repo.series_calls == []  # the database was never asked


def test_compare_two_periods(client):
    params = {
        "dp": "flow_temp",
        "a_from": "2026-10-10T00:00:00Z",
        "a_to": "2026-10-11T00:00:00Z",
        "b_from": "2026-10-17T00:00:00Z",
        "b_to": "2026-10-18T00:00:00Z",
    }

    body = client.get("/v1/plants/p-1/compare", params=params, headers=bearer()).json()

    assert body["period_a"][0]["offset_s"] == 0
    assert body["change_pct"] == 0.0


def test_all_business_routes_are_versioned(client):
    paths = client.get("/openapi.json").json()["paths"]

    business = [p for p in paths if p not in ("/health", "/ready")]
    assert business
    assert all(p.startswith("/v1/") for p in business)
