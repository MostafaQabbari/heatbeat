"""Shared test data and small database helpers.

What this file does: `make_message` builds one correct telemetry message (each
test changes exactly one thing in it), `provision` creates a customer with a
gateway (gateways must be known before they may send data), and `count` runs
a COUNT query.
"""

import json
from datetime import UTC, datetime
from typing import Any

import psycopg

from heatbeat.auth import Role, User, hash_password

NOW = datetime(2026, 10, 17, 14, 5, tzinfo=UTC)


def make_message(
    gateway: str = "gw-0042",
    plant: str = "p-17",
    seq: int = 1,
    boot_id: str = "boot-a",
    readings: list[dict[str, Any]] | None = None,
    at: datetime = NOW,
) -> dict[str, Any]:
    if readings is None:
        readings = [
            {"dp": "flow_temp", "t": at.isoformat(), "v": 62.4},
            {"dp": "return_temp", "t": at.isoformat(), "v": 41.8},
        ]
    return {
        "schema": 1,
        "gateway": gateway,
        "boot_id": boot_id,
        "seq": seq,
        "sent_at": at.isoformat(),
        "plant": plant,
        "readings": readings,
    }


def as_bytes(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode()


def topic_for(message: dict[str, Any]) -> str:
    return f"hb/v1/{message['gateway']}/plants/{message['plant']}/telemetry"


def count(database_url: str, sql: str) -> int:
    """Run a COUNT query against the test database."""
    with psycopg.connect(database_url) as conn:
        row = conn.execute(sql).fetchone()
        assert row is not None
        return int(row[0])


def provision(database_url: str, gateway: str = "gw-0042", tenant: str = "tenant-a") -> int:
    """Create the tenant (if needed) and the gateway; return the tenant id."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        tenant_id = conn.execute(
            "INSERT INTO tenant (key, name) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id",
            (tenant, tenant.upper()),
        ).fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO gateway (key, tenant_id) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (gateway, tenant_id)
        )
        return int(tenant_id)


def make_user(database_url: str, tenant_id: int, email: str, role: Role, password: str = "pw-12345") -> User:
    with psycopg.connect(database_url, autocommit=True) as conn:
        user_id = conn.execute(
            "INSERT INTO app_user (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant_id, email, hash_password(password), role.value),
        ).fetchone()[0]  # type: ignore[index]
    return User(id=int(user_id), tenant_id=tenant_id, email=email, role=role)
