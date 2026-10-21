"""Integration tests: optimiser runs, weather storage and the recommendation lifecycle (S19).

suggested -> approved (by an admin, becomes a normal command) -> applied (when the gateway confirms)
"""

from datetime import UTC, date, datetime, timedelta

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from helpers import count, make_message, make_user, provision, topic_for

from heatbeat.api.main import create_app
from heatbeat.auth import Role
from heatbeat.commands import CommandStore
from heatbeat.contract import CommandResult, TelemetryMessage
from heatbeat.db import PostgresSink
from heatbeat.external import OpenDataClient
from heatbeat.optimiser_jobs import fetch_weather, run_optimiser

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


@pytest.fixture
def warm_rooms(db) -> str:
    """Plant p-17 at a site: rooms around 25 degC for 60 hours, heating curve level 30, heating on."""
    tenant = provision(db, "gw-0042")
    readings = []
    for hour in range(60, 0, -1):
        t = (NOW - timedelta(hours=hour)).isoformat()
        readings.append({"dp": "room_temp", "t": t, "v": 25.0 + (hour % 3) * 0.1})
    t = (NOW - timedelta(minutes=5)).isoformat()
    readings += [{"dp": "heating_curve_level", "t": t, "v": 30.0}, {"dp": "heating_enabled", "t": t, "v": 1.0}]
    msg = make_message(readings=readings, at=NOW)
    sink = PostgresSink(db)
    sink.write([(topic_for(msg), TelemetryMessage.model_validate(msg))], [])
    sink.close()
    with psycopg.connect(db, autocommit=True) as conn:
        site = conn.execute(
            "INSERT INTO site (tenant_id, name, latitude, longitude) VALUES (%s, 'Site', 50.09, 9.06) RETURNING id",
            (tenant,),
        ).fetchone()[0]  # type: ignore[index]
        conn.execute("UPDATE plant SET site_id = %s WHERE key = 'p-17'", (site,))
    return db


def run(db: str) -> dict:
    with psycopg.connect(db, autocommit=True) as conn:
        return run_optimiser(conn, NOW)


def test_warm_rooms_give_a_heating_curve_recommendation(warm_rooms):
    summary = run(warm_rooms)

    assert summary["created"] == 1
    sql = "SELECT count(*) FROM recommendation WHERE kind = 'heating_curve' AND proposed_value = 27"
    assert count(warm_rooms, sql) == 1  # 30 -> 27: a 3 K step, not a jump


def test_s19_missing_forecast_is_visible_in_the_run_and_gives_no_season_advice(warm_rooms):
    summary = run(warm_rooms)

    assert summary["status"] == "partial"
    assert count(warm_rooms, "SELECT count(*) FROM recommendation WHERE kind = 'summer_shutdown'") == 0
    with psycopg.connect(warm_rooms) as conn:
        details = conn.execute("SELECT details FROM optimiser_run").fetchone()[0]  # type: ignore[index]
    assert any("forecast" in note for note in details["p-17"])


def test_warm_forecast_gives_summer_shutdown_and_reruns_do_not_duplicate(warm_rooms):
    with psycopg.connect(warm_rooms, autocommit=True) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO weather_forecast (site_id, ts, temperature, source) SELECT id, %s, 19.0, 'test' FROM site",
            [(NOW + timedelta(hours=h),) for h in range(72)],
        )

    run(warm_rooms)
    run(warm_rooms)

    assert count(warm_rooms, "SELECT count(*) FROM recommendation WHERE kind = 'summer_shutdown'") == 1
    assert count(warm_rooms, "SELECT count(*) FROM recommendation") == 2


def test_fetch_weather_stores_the_forecast_and_survives_a_dead_source(warm_rooms):
    sample = {"weather": [{"timestamp": (NOW + timedelta(hours=h)).isoformat(), "temperature": 12.0} for h in range(3)]}
    ok = OpenDataClient(
        "https://w.test",
        "https://p.test",
        httpx2.Client(transport=httpx2.MockTransport(lambda _r: httpx2.Response(200, json=sample))),
    )
    down = OpenDataClient(
        "https://w.test",
        "https://p.test",
        httpx2.Client(transport=httpx2.MockTransport(lambda _r: httpx2.Response(503))),
    )

    with psycopg.connect(warm_rooms, autocommit=True) as conn:
        stored = fetch_weather(conn, ok, date.today())
        failed = fetch_weather(conn, down, date.today())

    assert stored == {"stored": 3, "sites_failed": 0}
    assert failed == {"stored": 0, "sites_failed": 1}


def test_approved_recommendation_becomes_a_command_and_follows_its_result(warm_rooms, test_settings):
    run(warm_rooms)
    tenant = count(warm_rooms, "SELECT id FROM tenant")
    make_user(warm_rooms, tenant, "admin@a.example", Role.ADMIN, password="pw-12345")
    rec_id = count(warm_rooms, "SELECT id FROM recommendation")

    with TestClient(create_app(test_settings)) as client:
        token = client.post("/v1/auth/token", data={"username": "admin@a.example", "password": "pw-12345"}).json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        approved = client.post(f"/v1/recommendations/{rec_id}/approve", headers=headers).json()
        again = client.post(f"/v1/recommendations/{rec_id}/approve", headers=headers)

    assert approved["state"] == "approved"
    assert again.status_code == 409  # decided once
    sql = "SELECT count(*) FROM command WHERE source = 'optimiser' AND requested_value = 27 AND state = 'requested'"
    assert count(warm_rooms, sql) == 1

    result = CommandResult.model_validate(
        {
            "command_id": approved["command_id"],
            "gateway": "gw-0042",
            "status": "applied",
            "applied_value": 27.0,
            "at": datetime.now(UTC),
        }
    )
    CommandStore(psycopg.connect(warm_rooms, autocommit=True)).record_result(result)

    assert count(warm_rooms, "SELECT count(*) FROM recommendation WHERE state = 'applied'") == 1
