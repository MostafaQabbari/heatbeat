"""Fixtures for integration tests (real PostgreSQL + TimescaleDB).

What this file does: pytest loads it automatically. Integration tests get a
freshly migrated database; they are skipped if HB_TEST_DATABASE_URL is not set.
Start the database first:  docker compose up -d db mqtt
"""

import os
from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config


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
    """An empty database (plants, measurements, rejections) for each test."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("TRUNCATE measurement, rejected_message, plant RESTART IDENTITY CASCADE")
    yield migrated_db
