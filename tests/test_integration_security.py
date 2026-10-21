"""Integration tests: tenant isolation enforced by the DATABASE (scenario S12).

The API runs every query as the role `heatbeat_api` with `app.tenant_id` set.
These tests show that even a query WITHOUT any tenant filter only sees the
caller's own rows, and that the tests would notice if a policy were missing.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from helpers import make_message, make_user, provision, topic_for

from heatbeat.api.main import create_app
from heatbeat.auth import Role
from heatbeat.contract import TelemetryMessage
from heatbeat.db import PostgresSink

pytestmark = pytest.mark.integration


@pytest.fixture
def two_tenants(db) -> dict[str, int]:
    """Tenant A owns plant p-a (via gw-a), tenant B owns plant p-b (via gw-b)."""
    tenants = {"a": provision(db, "gw-a", "tenant-a"), "b": provision(db, "gw-b", "tenant-b")}
    sink = PostgresSink(db)
    for name in ("a", "b"):
        msg = make_message(gateway=f"gw-{name}", plant=f"p-{name}")
        sink.write([(topic_for(msg), TelemetryMessage.model_validate(msg))], [])
    sink.close()
    return tenants


@contextmanager
def as_tenant(db: str, tenant_id: int | None) -> Iterator[psycopg.Connection]:
    """A connection exactly like the API's: restricted role, tenant set for this transaction."""
    with psycopg.connect(db) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE heatbeat_api")
        if tenant_id is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield conn


def visible_plants(db: str, tenant_id: int | None) -> list[str]:
    with as_tenant(db, tenant_id) as conn:
        return [r[0] for r in conn.execute("SELECT key FROM plant ORDER BY key").fetchall()]  # NO tenant filter


def test_s12_query_without_tenant_filter_sees_only_own_plants(two_tenants, db):
    assert visible_plants(db, two_tenants["a"]) == ["p-a"]
    assert visible_plants(db, two_tenants["b"]) == ["p-b"]


def test_s12_no_tenant_set_means_no_rows(two_tenants, db):
    """Secure default: a forgotten set_config shows nothing, not everything."""
    assert visible_plants(db, None) == []


def test_s12_measurements_only_through_the_filtering_view(two_tenants, db):
    with as_tenant(db, two_tenants["a"]) as conn:
        plants = conn.execute(
            "SELECT DISTINCT p.key FROM measurement_visible m JOIN plant p ON p.id = m.plant_id"
        ).fetchall()
    assert plants == [("p-a",)]


def test_s12_raw_hypertable_is_closed_to_the_api_role(two_tenants, db):
    """Compressed hypertables cannot have row-level security, so the API role may not read them at all."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege), as_tenant(db, two_tenants["a"]) as conn:
        conn.execute("SELECT count(*) FROM measurement")


def test_password_hashes_are_unreadable_for_the_api(two_tenants, db):
    make_user(db, two_tenants["a"], "x@a.example", Role.ADMIN)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), as_tenant(db, two_tenants["a"]) as conn:
        conn.execute("SELECT password_hash FROM app_user")


def test_s12_cannot_write_rows_for_another_tenant(two_tenants, db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), as_tenant(db, two_tenants["a"]) as conn:
        conn.execute(
            "INSERT INTO command (tenant_id, plant_id, datapoint_id, requested_value, expected_version, "
            "state, expires_at) "
            "SELECT %s, 1, 1, 1, 0, 'requested', now()",
            (two_tenants["b"],),
        )


def test_mutation_check_the_test_notices_a_missing_policy(two_tenants, db):
    """Switch row-level security off (inside a transaction that is rolled back) and
    run the same check: it must now see the other tenant's plant. This proves the
    isolation tests above would fail if someone removed a policy."""
    with psycopg.connect(db) as conn:
        conn.execute("ALTER TABLE plant DISABLE ROW LEVEL SECURITY")
        conn.execute("SET ROLE heatbeat_api")
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(two_tenants["a"]),))
        leaked = [r[0] for r in conn.execute("SELECT key FROM plant ORDER BY key").fetchall()]
        conn.rollback()  # the policy is back

    assert leaked == ["p-a", "p-b"]  # without RLS the forgotten filter WOULD leak
    assert visible_plants(db, two_tenants["a"]) == ["p-a"]  # and it is protected again


def test_s12_api_returns_uniform_404_for_another_tenants_plant(two_tenants, db, test_settings):
    make_user(db, two_tenants["a"], "op@a.example", Role.OPERATOR, password="pw-a-12345")
    with TestClient(create_app(test_settings)) as client:
        token = client.post("/v1/auth/token", data={"username": "op@a.example", "password": "pw-a-12345"}).json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}

        own = client.get("/v1/plants", headers=headers).json()
        foreign = client.get("/v1/plants/p-b", headers=headers)
        missing = client.get("/v1/plants/does-not-exist", headers=headers)

    assert [p["key"] for p in own["items"]] == ["p-a"]
    assert foreign.status_code == missing.status_code == 404  # "exists elsewhere" looks like "does not exist"
    assert foreign.json()["title"] == missing.json()["title"]


def test_api_detail_shows_latest_values(two_tenants, db, test_settings):
    fresh = make_message(gateway="gw-a", plant="p-a", seq=2, at=datetime.now(UTC).replace(microsecond=0))
    sink = PostgresSink(db)
    sink.write([(topic_for(fresh), TelemetryMessage.model_validate(fresh))], [])
    sink.close()
    make_user(db, two_tenants["a"], "v@a.example", Role.VIEWER, password="pw-a-12345")

    with TestClient(create_app(test_settings)) as client:
        token = client.post("/v1/auth/token", data={"username": "v@a.example", "password": "pw-a-12345"}).json()
        body = client.get("/v1/plants/p-a", headers={"Authorization": f"Bearer {token['access_token']}"}).json()

    assert {v["datapoint"]: v["value"] for v in body["latest"]} == {"flow_temp": 62.4, "return_temp": 41.8}
    assert body["status"] == "online"
