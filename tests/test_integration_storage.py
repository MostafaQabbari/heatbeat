"""Integration tests: the database writer, migrations and query plans against real TimescaleDB.

Run with:  uv run pytest -m integration   (needs: docker compose up -d db mqtt)
"""

from datetime import timedelta

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from helpers import NOW, count, make_message, provision, topic_for

from heatbeat.api.repository import PostgresRepository
from heatbeat.api.schemas import Bucket
from heatbeat.contract import TelemetryMessage
from heatbeat.db import PostgresSink

pytestmark = pytest.mark.integration


def batch(**kwargs) -> list[tuple[str, TelemetryMessage]]:
    msg = make_message(**kwargs)
    return [(topic_for(msg), TelemetryMessage.model_validate(msg))]


@pytest.fixture
def sink(db):
    provision(db, "gw-0042")
    s = PostgresSink(db)
    yield s
    s.close()


def test_s11_migrations_go_down_and_up_cleanly(migrated_db):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_db)

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert count(migrated_db, "SELECT count(*) FROM timescaledb_information.hypertables") == 1


def test_s11_migration_0002_moves_existing_plants_into_a_default_tenant(migrated_db):
    """Expand -> backfill -> contract on live data: plants that existed before tenants keep working."""
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_db)
    command.downgrade(config, "0001")
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("INSERT INTO plant (key, gateway) VALUES ('old-plant', 'old-gw')")

    command.upgrade(config, "head")

    sql = "SELECT t.key FROM plant p JOIN tenant t ON t.id = p.tenant_id WHERE p.key = 'old-plant'"
    with psycopg.connect(migrated_db) as conn:
        assert conn.execute(sql).fetchone() == ("default",)
    assert count(migrated_db, "SELECT count(*) FROM gateway WHERE key = 'old-gw'") == 1


def test_batch_is_stored_and_plant_registered_in_the_gateways_tenant(sink, db):
    inserted = sink.write(batch(), [])

    assert inserted == 2
    sql = "SELECT count(*) FROM plant p JOIN tenant t ON t.id = p.tenant_id WHERE p.key = 'p-17' AND t.key = 'tenant-a'"
    assert count(db, sql) == 1


def test_unknown_gateway_is_rejected(sink, db):
    """Security: only provisioned gateways may deliver data."""
    sink.write(batch(gateway="gw-unknown"), [])

    assert count(db, "SELECT count(*) FROM measurement") == 0
    assert count(db, "SELECT count(*) FROM rejected_message WHERE reason LIKE 'unknown gateway%'") == 1


def test_s01_redelivered_batch_creates_no_duplicates(sink, db):
    """S01: MQTT QoS 1 is at-least-once; the unique key makes the second copy a no-op."""
    sink.write(batch(), [])
    second = sink.write(batch(), [])

    assert second == 0
    assert count(db, "SELECT count(*) FROM measurement") == 2


def test_s01_duplicates_are_ignored_even_in_compressed_chunks(sink, db):
    """Old data is compressed after 7 days; a very late redelivery must still be a no-op."""
    sink.write(batch(), [])
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("SELECT compress_chunk(c) FROM show_chunks('measurement') c")

    assert sink.write(batch(), []) == 0
    assert count(db, "SELECT count(*) FROM measurement") == 2


def test_plant_cannot_be_taken_over_by_another_gateway(sink, db):
    provision(db, "gw-0001")
    provision(db, "gw-0002")
    sink.write(batch(gateway="gw-0001"), [])

    sink.write(batch(gateway="gw-0002", readings=[{"dp": "flow_temp", "t": NOW.isoformat(), "v": 1.0}]), [])

    assert count(db, "SELECT count(*) FROM measurement") == 2
    assert count(db, "SELECT count(*) FROM rejected_message WHERE reason LIKE 'plant belongs%'") == 1


def test_unknown_datapoint_is_rejected_with_reason(sink, db):
    sink.write(batch(readings=[{"dp": "mystery", "t": NOW.isoformat(), "v": 1.0}]), [])

    assert count(db, "SELECT count(*) FROM measurement") == 0
    assert count(db, "SELECT count(*) FROM rejected_message WHERE reason LIKE 'unknown datapoint%'") == 1


def test_implausible_value_is_stored_with_flag(sink, db):
    sink.write(batch(readings=[{"dp": "flow_temp", "t": NOW.isoformat(), "v": 500.0}]), [])

    assert count(db, "SELECT count(*) FROM measurement WHERE quality = 2") == 1


def test_s02_broken_connection_fails_the_batch_then_recovers(sink, db):
    """S02: the connection dies; that write fails as a whole, the next one reconnects."""
    sink.write(batch(seq=1), [])
    with psycopg.connect(db, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )

    later = [{"dp": "flow_temp", "t": "2026-10-17T14:06:00Z", "v": 63.0}]
    with pytest.raises(psycopg.OperationalError):
        sink.write(batch(seq=2, readings=later), [])
    retried = sink.write(batch(seq=2, readings=later), [])

    assert retried == 1
    assert count(db, "SELECT count(*) FROM measurement") == 3


def test_main_query_uses_the_unique_index(sink, db):
    """Fitness function: the series query (plant + datapoint + time range) must not scan whole chunks."""
    for plant in range(30):
        provision(db, f"gw-{plant}")
        readings = [
            {"dp": dp, "t": (NOW - timedelta(minutes=i)).isoformat(), "v": 50.0}
            for i in range(100)
            for dp in ("flow_temp", "return_temp")
        ]
        sink.write(batch(gateway=f"gw-{plant}", plant=f"p-{plant}", readings=readings), [])
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("ANALYZE measurement")
        plan = "\n".join(
            row[0]
            for row in conn.execute(
                """
                EXPLAIN SELECT * FROM measurement
                WHERE plant_id = 1 AND datapoint_id = 2
                  AND ts >= '2026-10-17T13:00:00Z' AND ts < '2026-10-17T14:00:00Z'
                """
            )
        )

    assert "Seq Scan" not in plan, plan


def test_repository_reads_latest_values_and_series_through_the_tenant_views(sink, db):
    sink.write(batch(), [])
    tenant_id = count(db, "SELECT id FROM tenant WHERE key = 'tenant-a'")  # look it up BEFORE switching role
    with psycopg.connect(db, options="-c timezone=UTC") as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE heatbeat_api")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        repo = PostgresRepository(conn)

        raw = repo.series("p-17", ["flow_temp"], NOW - timedelta(hours=1), NOW + timedelta(hours=1), Bucket.MIN_5)
        hourly = repo.series("p-17", ["flow_temp"], NOW - timedelta(hours=3), NOW + timedelta(hours=3), Bucket.HOUR_1)
        plants = repo.list_plants(limit=10, after_id=0)

    assert [p.key for _, p in plants] == ["p-17"]
    assert raw[0].points[0].avg == pytest.approx(62.4)
    assert hourly[0].points[0].avg == pytest.approx(62.4)  # real-time aggregate includes fresh data
    assert raw[0].points[0].ts.utcoffset().total_seconds() == 0  # times come back in UTC
