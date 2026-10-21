"""Initial schema: plants, datapoint catalogue, measurement hypertable.

What this file does: creates the tables. `measurement` is a narrow TimescaleDB
hypertable (one row per value) with a unique key that makes duplicate MQTT
deliveries harmless. See docs/adr/0001-narrow-measurement-table.md.

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.execute("""
        CREATE TABLE plant (
            id           integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key          text NOT NULL UNIQUE,
            gateway      text NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz
        )
    """)

    op.execute("""
        CREATE TABLE datapoint (
            id            smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key           text NOT NULL UNIQUE,
            unit          text NOT NULL,
            description   text NOT NULL,
            min_plausible double precision NOT NULL,
            max_plausible double precision NOT NULL,
            CHECK (min_plausible < max_plausible)
        )
    """)

    op.execute("""
        INSERT INTO datapoint (key, unit, description, min_plausible, max_plausible) VALUES
            ('outdoor_temp',      'degC', 'Outdoor temperature',          -40, 50),
            ('flow_temp',         'degC', 'Flow (supply) temperature',      0, 110),
            ('return_temp',       'degC', 'Return temperature',             0, 110),
            ('burner_state',      '',     'Burner on (1) or off (0)',       0, 1),
            ('burner_modulation', '%',    'Burner modulation level',        0, 100)
    """)

    op.execute("""
        CREATE TABLE measurement (
            ts           timestamptz      NOT NULL,
            plant_id     integer          NOT NULL REFERENCES plant (id),
            datapoint_id smallint         NOT NULL REFERENCES datapoint (id),
            value        double precision NOT NULL,
            quality      smallint         NOT NULL DEFAULT 0,
            received_at  timestamptz      NOT NULL DEFAULT now(),
            CONSTRAINT measurement_unique UNIQUE (plant_id, datapoint_id, ts)
        )
    """)
    op.execute("SELECT create_hypertable('measurement', by_range('ts', INTERVAL '1 day'))")

    # Old chunks are compressed (per plant and datapoint, newest first) and
    # raw data older than a year is dropped.
    op.execute("""
        ALTER TABLE measurement SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'plant_id, datapoint_id',
            timescaledb.compress_orderby = 'ts DESC'
        )
    """)
    op.execute("SELECT add_compression_policy('measurement', INTERVAL '7 days')")
    op.execute("SELECT add_retention_policy('measurement', INTERVAL '365 days')")

    op.execute("""
        CREATE TABLE rejected_message (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            received_at timestamptz NOT NULL DEFAULT now(),
            topic       text NOT NULL,
            reason      text NOT NULL,
            payload     text NOT NULL
        )
    """)

    # A continuous aggregate cannot be created inside a transaction.
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE MATERIALIZED VIEW measurement_1h
            WITH (timescaledb.continuous) AS
            SELECT time_bucket(INTERVAL '1 hour', ts) AS bucket,
                   plant_id,
                   datapoint_id,
                   avg(value) AS avg_value,
                   min(value) AS min_value,
                   max(value) AS max_value,
                   count(*)   AS n
            FROM measurement
            GROUP BY bucket, plant_id, datapoint_id
            WITH NO DATA
        """)
        op.execute("""
            SELECT add_continuous_aggregate_policy('measurement_1h',
                start_offset      => INTERVAL '3 days',
                end_offset        => INTERVAL '1 hour',
                schedule_interval => INTERVAL '30 minutes')
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS measurement_1h")
    op.execute("DROP TABLE IF EXISTS rejected_message")
    op.execute("DROP TABLE IF EXISTS measurement")
    op.execute("DROP TABLE IF EXISTS datapoint")
    op.execute("DROP TABLE IF EXISTS plant")
