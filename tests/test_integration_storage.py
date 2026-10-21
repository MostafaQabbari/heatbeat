"""Integration tests: the database writer and the API repository against real TimescaleDB.

Run with:  uv run pytest -m integration   (needs: docker compose up -d db mqtt)
"""

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from helpers import NOW, count, make_message, topic_for
from psycopg_pool import ConnectionPool

from heatbeat.api.repository import PostgresRepository
from heatbeat.api.schemas import Bucket
from heatbeat.contract import TelemetryMessage
from heatbeat.db import PostgresSink
from heatbeat.ingest import QUALITY_IMPLAUSIBLE

pytestmark = pytest.mark.integration

NOW_READINGS = [
    {"dp": "flow_temp", "t": "2026-10-17T14:05:00Z", "v": 62.4},
    {"dp": "return_temp", "t": "2026-10-17T14:05:00Z", "v": 41.8},
]


def batch(**kwargs) -> list[tuple[str, TelemetryMessage]]:
    msg = make_message(**kwargs)
    return [(topic_for(msg), TelemetryMessage.model_validate(msg))]


@pytest.fixture
def sink(db):
    s = PostgresSink(db)
    yield s
    s.close()


def test_s11_migrations_go_down_and_up_cleanly(migrated_db):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_db)

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    hypertables = count(migrated_db, "SELECT count(*) FROM timescaledb_information.hypertables")
    assert hypertables == 1


def test_batch_is_stored_and_plant_registered(sink, db):
    inserted = sink.write(batch(), [])

    assert inserted == 2
    assert count(db, "SELECT count(*) FROM plant WHERE key = 'p-17'") == 1


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

    assert QUALITY_IMPLAUSIBLE == 2
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
    """The series query (plant + datapoint + time range) must not scan whole chunks."""
    from datetime import timedelta

    for plant in range(30):
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

    assert "measurement_unique" in plan or "Index" in plan, plan
    assert "Seq Scan" not in plan, plan


def test_repository_returns_latest_values_and_series(sink, db):
    sink.write(batch(), [])
    pool = ConnectionPool(db, min_size=1, max_size=2, open=True, kwargs={"options": "-c timezone=UTC"})
    try:
        repo = PostgresRepository(pool)

        series = repo.series(
            "p-17",
            ["flow_temp"],
            NOW.replace(hour=0, minute=0),
            NOW.replace(hour=23, minute=0),
            Bucket.HOUR_1,
        )
        plants = repo.list_plants(limit=10, after_id=0)
    finally:
        pool.close()

    assert [p.key for _, p in plants] == ["p-17"]
    assert series[0].datapoint == "flow_temp"
    assert series[0].points[0].avg == pytest.approx(62.4)
    assert series[0].points[0].ts.utcoffset().total_seconds() == 0  # times come back in UTC
