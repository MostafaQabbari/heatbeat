"""Data access for the API.

What this file does: all SQL the API needs, behind a small interface
(`PlantRepository`). Routes never contain SQL, and tests can swap in a fake.
"""

from datetime import datetime
from typing import Protocol

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from heatbeat.api.schemas import Bucket, LatestValue, PlantSummary, Series, SeriesPoint


class PlantRepository(Protocol):
    def ping(self) -> bool: ...
    def list_plants(self, limit: int, after_id: int) -> list[tuple[int, PlantSummary]]: ...
    def get_plant(self, key: str) -> PlantSummary | None: ...
    def latest_values(self, key: str) -> list[LatestValue]: ...
    def datapoint_keys(self) -> set[str]: ...
    def series(
        self, key: str, datapoints: list[str], start: datetime, end: datetime, bucket: Bucket
    ) -> list[Series]: ...


class PostgresRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def ping(self) -> bool:
        with self._pool.connection() as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)

    def list_plants(self, limit: int, after_id: int) -> list[tuple[int, PlantSummary]]:
        # Keyset (cursor) pagination: fast on any page, stable while rows are added.
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT id, key, gateway, created_at, last_seen_at FROM plant
                WHERE id > %s ORDER BY id LIMIT %s
                """,
                (after_id, limit),
            ).fetchall()
        return [(r.pop("id"), PlantSummary(**r)) for r in rows]

    def get_plant(self, key: str) -> PlantSummary | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT key, gateway, created_at, last_seen_at FROM plant WHERE key = %s", (key,)
            ).fetchone()
        return PlantSummary(**row) if row else None

    def latest_values(self, key: str) -> list[LatestValue]:
        # Newest value per datapoint, looking back one day only (keeps the scan small).
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT DISTINCT ON (d.key)
                       d.key AS datapoint, d.unit, m.ts, m.value, m.quality
                FROM measurement m
                JOIN plant p     ON p.id = m.plant_id
                JOIN datapoint d ON d.id = m.datapoint_id
                WHERE p.key = %s AND m.ts > now() - INTERVAL '1 day'
                ORDER BY d.key, m.ts DESC
                """,
                (key,),
            ).fetchall()
        return [LatestValue(**r) for r in rows]

    def datapoint_keys(self) -> set[str]:
        with self._pool.connection() as conn:
            return {row[0] for row in conn.execute("SELECT key FROM datapoint").fetchall()}

    def series(
        self, key: str, datapoints: list[str], start: datetime, end: datetime, bucket: Bucket
    ) -> list[Series]:
        # Only good-quality values (quality = 0) go into the aggregates.
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT d.key AS datapoint, d.unit,
                       time_bucket(%(width)s, m.ts) AS ts,
                       avg(m.value) AS avg, min(m.value) AS min, max(m.value) AS max,
                       count(*) AS count
                FROM measurement m
                JOIN plant p     ON p.id = m.plant_id
                JOIN datapoint d ON d.id = m.datapoint_id
                WHERE p.key = %(plant)s
                  AND d.key = ANY(%(dps)s)
                  AND m.ts >= %(start)s AND m.ts < %(end)s
                  AND m.quality = 0
                GROUP BY d.key, d.unit, 3
                ORDER BY d.key, 3
                """,
                {"width": bucket.interval, "plant": key, "dps": datapoints, "start": start, "end": end},
            ).fetchall()

        by_dp: dict[str, Series] = {}
        for r in rows:
            series = by_dp.setdefault(
                r["datapoint"], Series(datapoint=r["datapoint"], unit=r["unit"], bucket=bucket, points=[])
            )
            series.points.append(
                SeriesPoint(ts=r["ts"], avg=r["avg"], min=r["min"], max=r["max"], count=r["count"])
            )
        return list(by_dp.values())
