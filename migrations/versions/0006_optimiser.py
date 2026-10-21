"""External data (weather forecast, electricity prices) and recommendations.

What this file does: stores the weather forecast per site and day-ahead
prices, logs every optimiser run (inputs, version, result, so any
recommendation can be reproduced), and keeps recommendations with their
lifecycle: suggested -> approved -> applied -> measured (or dismissed).
Recommendations are derived data: they can always be recomputed from the
stored measurements and inputs (DDIA ch. 1, systems of record and derived data).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE weather_forecast (
            site_id     integer NOT NULL REFERENCES site (id) ON DELETE CASCADE,
            ts          timestamptz NOT NULL,
            temperature double precision NOT NULL,
            source      text NOT NULL,
            fetched_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, ts)
        )
    """)
    op.execute("""
        CREATE TABLE energy_price (
            zone        text NOT NULL,
            ts          timestamptz NOT NULL,
            eur_per_mwh double precision NOT NULL,
            source      text NOT NULL,
            fetched_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (zone, ts)
        )
    """)
    op.execute("""
        CREATE TABLE optimiser_run (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind        text NOT NULL,
            version     text NOT NULL,
            started_at  timestamptz NOT NULL,
            finished_at timestamptz,
            status      text NOT NULL CHECK (status IN ('ok', 'partial', 'skipped', 'failed')),
            details     jsonb NOT NULL DEFAULT '{}'
        )
    """)
    op.execute("""
        CREATE TABLE recommendation (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id       integer  NOT NULL REFERENCES tenant (id),
            plant_id        integer  NOT NULL REFERENCES plant (id),
            run_id          bigint   REFERENCES optimiser_run (id),
            kind            text     NOT NULL CHECK (kind IN ('heating_curve', 'summer_shutdown', 'heating_restart')),
            title           text     NOT NULL,
            rationale       text     NOT NULL,
            datapoint_id    smallint NOT NULL REFERENCES datapoint (id),
            current_value   double precision,
            proposed_value  double precision NOT NULL,
            state           text NOT NULL DEFAULT 'suggested'
                CHECK (state IN ('suggested', 'approved', 'applied', 'measured', 'dismissed')),
            command_id      uuid REFERENCES command (id),
            decided_by      integer REFERENCES app_user (id),
            decided_at      timestamptz,
            applied_at      timestamptz,
            measured_effect text,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX recommendation_one_open ON recommendation (plant_id, kind) WHERE state = 'suggested'"
    )
    op.execute("ALTER TABLE recommendation ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON recommendation "
        "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
    )
    op.execute("GRANT SELECT ON recommendation, weather_forecast, energy_price TO heatbeat_api")
    op.execute("GRANT UPDATE (state, decided_by, decided_at, command_id) ON recommendation TO heatbeat_api")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS recommendation, optimiser_run, energy_price, weather_forecast")
