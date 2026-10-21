"""Scheduled data jobs: fetch weather and prices, run the optimisers, measure results.

What this file does: the database side of the optimiser. Each job is a plain
function (easy to test and to run by hand); worker.py only schedules them.
Every optimiser run is logged in `optimiser_run` with its version and, per
plant, why a recommendation was or was not made.
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from heatbeat.external import OpenDataClient, SourceUnavailable
from heatbeat.optimiser import VERSION, Proposal, Skipped, heating_curve, season_switch
from heatbeat.savings import Day, NotEnoughData, estimate

log = logging.getLogger("heatbeat.optimiser")

ENERGY_DATAPOINT = {"gas_boiler": "burner_power_kw", "heat_pump": "electrical_power_kw"}


def fetch_weather(conn: psycopg.Connection, client: OpenDataClient, today: date) -> dict[str, int]:
    """Store a 3-day forecast for every site with coordinates."""
    stored, failed = 0, 0
    sites = conn.execute("SELECT id, latitude, longitude FROM site WHERE latitude IS NOT NULL").fetchall()
    for site_id, lat, lon in sites:
        try:
            hours = client.forecast(lat, lon, today, today + timedelta(days=3))
        except SourceUnavailable as exc:
            log.warning("forecast for site %s unavailable: %s", site_id, exc)
            failed += 1
            continue
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO weather_forecast (site_id, ts, temperature, source) VALUES (%s, %s, %s, 'dwd-brightsky')
                ON CONFLICT (site_id, ts) DO UPDATE SET temperature = EXCLUDED.temperature, fetched_at = now()
                """,
                [(site_id, h.ts, h.temperature) for h in hours],
            )
        stored += len(hours)
    return {"stored": stored, "sites_failed": failed}


def fetch_prices(conn: psycopg.Connection, client: OpenDataClient, today: date) -> int:
    prices = client.day_ahead_prices(today, today + timedelta(days=1))
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO energy_price (zone, ts, eur_per_mwh, source) VALUES ('DE-LU', %s, %s, 'energy-charts')
            ON CONFLICT (zone, ts) DO UPDATE SET eur_per_mwh = EXCLUDED.eur_per_mwh, fetched_at = now()
            """,
            [(p.ts, p.eur_per_mwh) for p in prices],
        )
    return len(prices)


def _latest(conn: psycopg.Connection, plant_id: int, dp: str) -> float | None:
    row = conn.execute(
        """
        SELECT m.value FROM measurement m JOIN datapoint d ON d.id = m.datapoint_id
        WHERE m.plant_id = %s AND d.key = %s AND m.ts > now() - INTERVAL '1 day'
        ORDER BY m.ts DESC LIMIT 1
        """,
        (plant_id, dp),
    ).fetchone()
    return float(row[0]) if row else None


def _hourly(conn: psycopg.Connection, plant_id: int, dp: str, since: datetime) -> list[float]:
    rows = conn.execute(
        """
        SELECT c.avg_value FROM measurement_1h c JOIN datapoint d ON d.id = c.datapoint_id
        WHERE c.plant_id = %s AND d.key = %s AND c.bucket >= %s ORDER BY c.bucket
        """,
        (plant_id, dp, since),
    ).fetchall()
    return [float(r[0]) for r in rows]


def run_optimiser(conn: psycopg.Connection, now: datetime) -> dict[str, Any]:
    """Run both optimisers for every plant. Returns a summary (also stored in optimiser_run)."""
    with conn.transaction():
        run_id = conn.execute(
            "INSERT INTO optimiser_run (kind, version, started_at, status) VALUES ('daily', %s, %s, 'ok') RETURNING id",
            (VERSION, now),
        ).fetchone()[0]  # type: ignore[index]
    details: dict[str, list[str]] = {}
    created = 0
    missing_forecast = False
    plants = conn.execute("SELECT id, key, tenant_id, site_id FROM plant ORDER BY id").fetchall()
    for plant_id, key, tenant_id, site_id in plants:
        results: list[Proposal | Skipped] = [
            heating_curve(
                _hourly(conn, plant_id, "room_temp", now - timedelta(days=3)),
                _latest(conn, plant_id, "heating_curve_level"),
            ),
        ]
        if site_id is None:
            results.append(Skipped("plant has no site location, so no forecast"))
        else:
            forecast = conn.execute(
                "SELECT ts, temperature FROM weather_forecast WHERE site_id = %s AND ts >= %s AND ts < %s ORDER BY ts",
                (site_id, now, now + timedelta(hours=72)),
            ).fetchall()
            missing_forecast = missing_forecast or len(forecast) < 48
            results.append(season_switch([(ts, t) for ts, t in forecast], _latest(conn, plant_id, "heating_enabled")))

        notes = []
        for result in results:
            if isinstance(result, Skipped):
                notes.append(f"skipped: {result.reason}")
                continue
            with conn.transaction():
                cur = conn.execute(
                    """
                    INSERT INTO recommendation (tenant_id, plant_id, run_id, kind, title, rationale,
                                                datapoint_id, current_value, proposed_value)
                    SELECT %s, %s, %s, %s, %s, %s, d.id, %s, %s FROM datapoint d WHERE d.key = %s
                    ON CONFLICT (plant_id, kind) WHERE state = 'suggested' DO NOTHING
                    """,
                    (
                        tenant_id,
                        plant_id,
                        run_id,
                        result.kind,
                        result.title,
                        result.rationale,
                        result.current_value,
                        result.proposed_value,
                        result.datapoint,
                    ),
                )
            created += cur.rowcount
            notes.append(f"{result.kind}: {result.title}")
        details[key] = notes

    status = "partial" if missing_forecast else "ok"  # S19: missing input is visible, not silent
    with conn.transaction():
        conn.execute(
            "UPDATE optimiser_run SET finished_at = %s, status = %s, details = %s WHERE id = %s",
            (datetime.now(UTC), status, json.dumps(details), run_id),
        )
    log.info("optimiser run %s: %d new recommendation(s), status %s", run_id, created, status)
    return {"run_id": run_id, "created": created, "status": status}


def daily_energy(conn: psycopg.Connection, plant_id: int, plant_type: str, start: datetime, end: datetime) -> list[Day]:
    """Daily energy (kWh, from hourly mean power) and mean outdoor temperature."""
    dp = ENERGY_DATAPOINT.get(plant_type)
    if dp is None:
        return []
    rows = conn.execute(
        """
        SELECT time_bucket(INTERVAL '1 day', c.bucket) AS day,
               avg(c.avg_value) FILTER (WHERE d.key = 'outdoor_temp')  AS outdoor,
               sum(c.avg_value) FILTER (WHERE d.key = %(dp)s)          AS kwh,   -- kW x 1 h
               count(*)         FILTER (WHERE d.key = %(dp)s)          AS hours
        FROM measurement_1h c JOIN datapoint d ON d.id = c.datapoint_id
        WHERE c.plant_id = %(plant)s AND c.bucket >= %(start)s AND c.bucket < %(end)s
          AND d.key IN ('outdoor_temp', %(dp)s)
        GROUP BY 1 ORDER BY 1
        """,
        {"dp": dp, "plant": plant_id, "start": start, "end": end},
    ).fetchall()
    return [
        Day(float(outdoor), float(kwh))
        for _, outdoor, kwh, hours in rows
        if hours and hours >= 20 and outdoor is not None
    ]


def measure_recommendations(conn: psycopg.Connection, now: datetime) -> int:
    """For changes applied at least 7 days ago: estimate the weather-normalised effect."""
    rows = conn.execute(
        """
        SELECT r.id, r.plant_id, p.plant_type, r.applied_at FROM recommendation r JOIN plant p ON p.id = r.plant_id
        WHERE r.state = 'applied' AND r.applied_at < %s
        """,
        (now - timedelta(days=7),),
    ).fetchall()
    measured = 0
    for rec_id, plant_id, plant_type, applied_at in rows:
        before = daily_energy(conn, plant_id, plant_type, applied_at - timedelta(days=28), applied_at)
        after = daily_energy(conn, plant_id, plant_type, applied_at + timedelta(days=1), now)
        try:
            text = estimate(before, after).summary()
        except NotEnoughData as exc:
            if now - applied_at < timedelta(days=60):
                continue  # try again later
            text = f"not measurable: {exc}"
        with conn.transaction():
            conn.execute(
                "UPDATE recommendation SET state = 'measured', measured_effect = %s WHERE id = %s",
                (text, rec_id),
            )
        measured += 1
    return measured
