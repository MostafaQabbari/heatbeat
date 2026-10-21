"""Fixtures for integration tests (real PostgreSQL + TimescaleDB, real Mosquitto).

What this file does: pytest loads it automatically. Integration tests get a
freshly migrated, empty database; they are skipped if HB_TEST_DATABASE_URL is
not set. Start the services first:  docker compose up -d db mqtt
"""

import os
from collections.abc import Iterator
from urllib.parse import urlparse

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from heatbeat.config import Settings

TABLES = (
    "measurement, rejected_message, notification_outbox, alarm_event, alarm, alarm_rule, parameter_audit, "
    "parameter, recommendation, optimiser_run, command, weather_forecast, energy_price, plant, gateway, site, "
    "app_user, tenant"
)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("HB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HB_TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> Iterator[str]:
    """Run all migrations up once per test session, and down again at the end."""
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


@pytest.fixture
def db(migrated_db: str) -> Iterator[str]:
    """An empty database for each test (the datapoint catalogue stays)."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    yield migrated_db


@pytest.fixture
def test_settings(database_url: str) -> Settings:
    """Settings that point at the test database (for the API and the workers)."""
    url = urlparse(database_url)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_host=url.hostname or "127.0.0.1",
        db_port=url.port or 5432,
        db_name=url.path.lstrip("/"),
        db_user=url.username or "heatbeat",
        db_password=url.password or "",
        jwt_secret="integration-test-secret-0123456789abcdef",
        mqtt_host=os.environ.get("HB_TEST_MQTT_HOST", "127.0.0.1"),
    )
