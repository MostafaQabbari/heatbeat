"""SQL for plants, latest values and time series.

What this file does: all plant-related SQL, behind a small interface so routes
contain no SQL and tests can use a fake. NOTE: there is deliberately no
"WHERE tenant_id = ..." here. The connection runs as `heatbeat_api` with the
tenant set, so row-level security and the *_visible views do the filtering
(scenario S12: a forgotten filter cannot leak another customer's data).
"""

from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from heatbeat.api.schemas import (
    Bucket,
    LatestValue,
    Parameter,
    PlantStatus,
    PlantSummary,
    Series,
    SeriesPoint,
    Site,
)

STATUS_SQL = """
    CASE WHEN p.last_seen_at IS NULL THEN 'never'
         WHEN p.last_seen_at > now() - INTERVAL '5 minutes' THEN 'online'
         ELSE 'offline' END
"""

# Built only from constants in this file; values are always passed as parameters.
PLANT_SELECT = f"""
    SELECT p.id, p.key, p.name, p.plant_type, p.gateway, p.created_at, p.last_seen_at,
           {STATUS_SQL} AS status,
           s.name AS site_name, s.address, s.latitude, s.longitude,
           {{open_alarms}} AS open_alarms
    FROM plant p
    LEFT JOIN site s ON s.id = p.site_id
"""  # noqa: S608


class PlantRepository(Protocol):
    def ping(self) -> bool: ...
    def list_plants(
        self,
        limit: int,
        after_id: int,
        plant_type: str | None = None,
        status: PlantStatus | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> list[tuple[int, PlantSummary]]: ...
    def get_plant(self, key: str) -> PlantSummary | None: ...
    def latest_values(self, key: str) -> list[LatestValue]: ...
    def parameters(self, key: str) -> list[Parameter]: ...
    def datapoint_keys(self) -> set[str]: ...
    def series(
        self, key: str, datapoints: list[str], start: datetime, end: datetime, bucket: Bucket
    ) -> list[Series]: ...


def to_summary(row: dict[str, Any]) -> PlantSummary:
    site = None
    if row["site_name"] is not None:
        site = Site(
            name=row["site_name"],
            address=row["address"],
            latitude=row["latitude"],
            longitude=row["longitude"],
        )
    return PlantSummary(
        key=row["key"],
        name=row["name"],
        plant_type=row["plant_type"],
        gateway=row["gateway"],
        status=PlantStatus(row["status"]),
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        site=site,
        open_alarms=row["open_alarms"],
    )


class PostgresRepository:
    def __init__(self, conn: psycopg.Connection, open_alarms_sql: str = "0") -> None:
        self._conn = conn
        self._plant_select = PLANT_SELECT.format(open_alarms=open_alarms_sql)

    def ping(self) -> bool:
        return self._conn.execute("SELECT 1").fetchone() == (1,)

    def list_plants(
        self,
        limit: int,
        after_id: int,
        plant_type: str | None = None,
        status: PlantStatus | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> list[tuple[int, PlantSummary]]:
        # Keyset (cursor) pagination: fast on any page, stable while rows are added.
        min_lon, min_lat, max_lon, max_lat = bbox or (0.0, 0.0, 0.0, 0.0)
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                self._plant_select
                + f"""
                WHERE p.id > %(after)s
                  AND (%(ptype)s::text IS NULL OR p.plant_type = %(ptype)s)
                  AND (%(status)s::text IS NULL OR {STATUS_SQL} = %(status)s)
                  AND (NOT %(use_bbox)s OR (s.longitude BETWEEN %(min_lon)s AND %(max_lon)s
                                           AND s.latitude BETWEEN %(min_lat)s AND %(max_lat)s))
                ORDER BY p.id
                LIMIT %(limit)s
                """,
                {
                    "after": after_id,
                    "ptype": plant_type,
                    "status": status.value if status else None,
                    "use_bbox": bbox is not None,
                    "min_lon": min_lon,
                    "min_lat": min_lat,
                    "max_lon": max_lon,
                    "max_lat": max_lat,
                    "limit": limit,
                },
            ).fetchall()
        return [(r["id"], to_summary(r)) for r in rows]

    def get_plant(self, key: str) -> PlantSummary | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(self._plant_select + " WHERE p.key = %s", (key,)).fetchone()
        return to_summary(row) if row else None

    def latest_values(self, key: str) -> list[LatestValue]:
        # Newest value per datapoint, looking back one day only (keeps the scan small).
        # Filtering on plant_id (not on a join with plant.key) lets the condition reach the
        # unique index inside the security-barrier view: about 250 ms -> 60-90 ms on 2 million rows
        # (docs/query-plans.md).
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT DISTINCT ON (d.key)
                       d.key AS datapoint, d.unit, m.ts, m.value, m.quality
                FROM measurement_visible m
                JOIN datapoint d ON d.id = m.datapoint_id
                WHERE m.plant_id = (SELECT id FROM plant WHERE key = %s)
                  AND m.ts > now() - INTERVAL '1 day'
                ORDER BY d.key, m.ts DESC
                """,
                (key,),
            ).fetchall()
        return [LatestValue(**r) for r in rows]

    def parameters(self, key: str) -> list[Parameter]:
        from heatbeat.api.stores import CommandApiStore

        return CommandApiStore(self._conn).parameters(key)

    def datapoint_keys(self) -> set[str]:
        return {row[0] for row in self._conn.execute("SELECT key FROM datapoint").fetchall()}

    def series(self, key: str, datapoints: list[str], start: datetime, end: datetime, bucket: Bucket) -> list[Series]:
        params = {"width": bucket.interval, "plant": key, "dps": datapoints, "start": start, "end": end}
        if bucket.uses_hourly_aggregate:
            # Dashboards over long periods read the pre-aggregated hourly view, not raw rows.
            sql = """
                SELECT d.key AS datapoint, d.unit, time_bucket(%(width)s, c.bucket) AS ts,
                       sum(c.avg_value * c.n) / sum(c.n) AS avg,
                       min(c.min_value) AS min, max(c.max_value) AS max, sum(c.n)::bigint AS count
                FROM measurement_1h_visible c
                JOIN plant p     ON p.id = c.plant_id
                JOIN datapoint d ON d.id = c.datapoint_id
                WHERE p.key = %(plant)s AND d.key = ANY(%(dps)s)
                  AND c.bucket >= %(start)s AND c.bucket < %(end)s
                GROUP BY d.key, d.unit, 3
                ORDER BY d.key, 3
            """
        else:
            # Only good-quality values (quality = 0) go into the aggregates.
            sql = """
                SELECT d.key AS datapoint, d.unit, time_bucket(%(width)s, m.ts) AS ts,
                       avg(m.value) AS avg, min(m.value) AS min, max(m.value) AS max, count(*) AS count
                FROM measurement_visible m
                JOIN plant p     ON p.id = m.plant_id
                JOIN datapoint d ON d.id = m.datapoint_id
                WHERE p.key = %(plant)s AND d.key = ANY(%(dps)s)
                  AND m.ts >= %(start)s AND m.ts < %(end)s
                  AND m.quality = 0
                GROUP BY d.key, d.unit, 3
                ORDER BY d.key, 3
            """
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(sql, params).fetchall()

        by_dp: dict[str, Series] = {}
        for r in rows:
            series = by_dp.setdefault(
                r["datapoint"], Series(datapoint=r["datapoint"], unit=r["unit"], bucket=bucket, points=[])
            )
            series.points.append(SeriesPoint(ts=r["ts"], avg=r["avg"], min=r["min"], max=r["max"], count=r["count"]))
        return list(by_dp.values())
