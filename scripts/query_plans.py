"""Write docs/query-plans.md: EXPLAIN ANALYZE of the API's main queries, run as the API role.

What this file does: picks one plant with data, switches to the restricted API role
with that plant's tenant (exactly like a request does), runs the three hot queries
of the API with EXPLAIN (ANALYZE, BUFFERS) and stores the plans with a short note.
Run: uv run python scripts/query_plans.py   (needs a database with simulator data)
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from heatbeat.config import Settings

QUERIES = {
    "Latest value per datapoint, FIRST version: filter through a join on plant.key": """
        SELECT DISTINCT ON (d.key) d.key, m.ts, m.value
        FROM measurement_visible m
        JOIN plant p ON p.id = m.plant_id
        JOIN datapoint d ON d.id = m.datapoint_id
        WHERE p.key = %(plant)s AND m.ts > now() - INTERVAL '1 day'
        ORDER BY d.key, m.ts DESC
    """,
    "Latest value per datapoint, CURRENT version: filter on plant_id (plant detail page)": """
        SELECT DISTINCT ON (d.key) d.key, m.ts, m.value
        FROM measurement_visible m
        JOIN datapoint d ON d.id = m.datapoint_id
        WHERE m.plant_id = (SELECT id FROM plant WHERE key = %(plant)s)
          AND m.ts > now() - INTERVAL '1 day'
        ORDER BY d.key, m.ts DESC
    """,
    "Raw series, 6 hours in 5-minute buckets (trend chart, short range)": """
        SELECT d.key, time_bucket('5 minutes', m.ts) AS ts, avg(m.value), min(m.value), max(m.value)
        FROM measurement_visible m
        JOIN plant p ON p.id = m.plant_id
        JOIN datapoint d ON d.id = m.datapoint_id
        WHERE p.key = %(plant)s AND d.key = ANY(%(dps)s)
          AND m.ts >= now() - INTERVAL '6 hours' AND m.ts < now() AND m.quality = 0
        GROUP BY 1, 2 ORDER BY 1, 2
    """,
    "Hourly aggregate, 7 days (trend chart, long range)": """
        SELECT d.key, c.bucket, c.avg_value, c.min_value, c.max_value
        FROM measurement_1h_visible c
        JOIN plant p ON p.id = c.plant_id
        JOIN datapoint d ON d.id = c.datapoint_id
        WHERE p.key = %(plant)s AND d.key = ANY(%(dps)s)
          AND c.bucket >= now() - INTERVAL '7 days' AND c.bucket < now()
        ORDER BY 1, 2
    """,
}


def main() -> None:
    settings = Settings()
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            """
            SELECT p.key, p.tenant_id, (SELECT count(*) FROM measurement) AS rows
            FROM plant p WHERE p.last_seen_at IS NOT NULL AND p.tenant_id IS NOT NULL
            ORDER BY p.key LIMIT 1
            """
        ).fetchone()
        if row is None:
            sys.exit("no plant with data: run the simulator first")
        plant, tenant, total = row
        version = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
        params = {"plant": plant, "dps": ["flow_temp", "return_temp", "outdoor_temp"]}

        out = [
            "# Query plans",
            "",
            "What this file does: shows how PostgreSQL/TimescaleDB executes the API's hot queries,",
            "run as the restricted API role through the tenant-filtering views (ADR 0005).",
            f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `scripts/query_plans.py`; "
            f"{total:,} measurement rows; TimescaleDB {version[0] if version else '?'}; plant `{plant}`.",
            "",
            "What to look for: chunk exclusion (only recent chunks appear), index scans on",
            "the `measurement_unique` key (plant_id, datapoint_id, ts) that also makes writes idempotent,",
            "and the hourly view reading the continuous aggregate instead of raw rows.",
            "",
            "Findings while writing this file:",
            "- The first version of the latest-value query joined `plant` by key. Through the",
            "  security-barrier view that condition did not reach the index, so it read the last day",
            "  of ALL the tenant's plants. Filtering on `plant_id` lets it use the unique index",
            "  (compare the first two plans). It still sorts one day of one plant; a per-datapoint",
            "  `LATERAL ... ORDER BY ts DESC LIMIT 1` was tried and was not faster through the view.",
            "- PostgreSQL's JIT compiler is on by default and once added about 0.4 s to a 20 ms query",
            "  whose cost estimate was high. For short API queries `jit = off` for the API role is worth",
            "  measuring.",
            "- The hourly view is a real-time continuous aggregate: materialized hours plus raw rows",
            "  since the last refresh (`materialized_only = false`), hence the UNION in its plan.",
        ]
        for title, sql in QUERIES.items():
            with conn.transaction():
                conn.execute("SET LOCAL ROLE heatbeat_api")
                conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
                plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql, params).fetchall()  # type: ignore[operator]
            out += [
                "",
                f"## {title}",
                "",
                "```sql",
                sql.strip("\n").rstrip(),
                "```",
                "",
                "```",
                *(r[0] for r in plan),
                "```",
            ]

    Path("docs/query-plans.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("wrote docs/query-plans.md")


if __name__ == "__main__":
    main()
