"""Shared test data.

What this file does: `make_message` builds one correct telemetry message.
Each test changes exactly one thing in it, so a failure points to one cause.
"""

import json
from datetime import UTC, datetime
from typing import Any

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
    import psycopg

    with psycopg.connect(database_url) as conn:
        row = conn.execute(sql).fetchone()
        assert row is not None
        return int(row[0])
