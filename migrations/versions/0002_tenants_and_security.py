"""Tenants, users, sites, gateways, and tenant isolation in the database.

What this file does: adds customers (tenants), users with roles, sites with
coordinates, and provisioned gateways. Existing plants are moved into a
"default" tenant (expand, backfill, then make NOT NULL). The API runs as the
role `heatbeat_api`, which:
- sees relational tables only through row-level security policies, and
- has NO access to the measurement hypertable, only to two views that filter
  by tenant. TimescaleDB 2.17 does not allow row-level security on a
  compressed hypertable (verified), see docs/adr/0005-tenant-isolation.md.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

RLS_TABLES = ["site", "gateway", "plant", "app_user"]


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION current_tenant() RETURNS integer
        LANGUAGE sql STABLE
        AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::integer $$
    """)

    op.execute("""
        CREATE TABLE tenant (
            id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key        text NOT NULL UNIQUE,
            name       text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE app_user (
            id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id     integer NOT NULL REFERENCES tenant (id),
            email         text NOT NULL UNIQUE CHECK (email = lower(email)),
            password_hash text NOT NULL,
            role          text NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE site (
            id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id integer NOT NULL REFERENCES tenant (id),
            name      text NOT NULL,
            address   text,
            latitude  double precision CHECK (latitude BETWEEN -90 AND 90),
            longitude double precision CHECK (longitude BETWEEN -180 AND 180)
        )
    """)
    op.execute("""
        CREATE TABLE gateway (
            id           integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key          text NOT NULL UNIQUE,
            tenant_id    integer NOT NULL REFERENCES tenant (id),
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz
        )
    """)

    # Expand: new nullable columns on plant.
    op.execute("""
        ALTER TABLE plant
            ADD COLUMN tenant_id  integer REFERENCES tenant (id),
            ADD COLUMN site_id    integer REFERENCES site (id),
            ADD COLUMN plant_type text NOT NULL DEFAULT 'unknown'
                CHECK (plant_type IN ('gas_boiler', 'heat_pump', 'district_heating', 'unknown')),
            ADD COLUMN name       text
    """)

    # Backfill: plants that already exist move into a default tenant.
    op.execute("""
        INSERT INTO tenant (key, name)
        SELECT 'default', 'Default tenant' WHERE EXISTS (SELECT 1 FROM plant)
    """)
    op.execute("""
        INSERT INTO gateway (key, tenant_id)
        SELECT DISTINCT p.gateway, t.id FROM plant p CROSS JOIN tenant t WHERE t.key = 'default'
    """)
    op.execute("UPDATE plant SET tenant_id = (SELECT id FROM tenant WHERE key = 'default')")

    # Contract: now every plant has a tenant and a known gateway.
    op.execute("ALTER TABLE plant ALTER COLUMN tenant_id SET NOT NULL")
    op.execute("ALTER TABLE plant ADD CONSTRAINT plant_gateway_fk FOREIGN KEY (gateway) REFERENCES gateway (key)")
    op.execute("CREATE INDEX plant_tenant_idx ON plant (tenant_id, id)")

    # Rebuild the hourly aggregate: only good-quality values (like the raw queries),
    # and real-time aggregation, so the newest, not yet materialised hour is included.
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW measurement_1h")
        op.execute("""
            CREATE MATERIALIZED VIEW measurement_1h
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket(INTERVAL '1 hour', ts) AS bucket,
                   plant_id, datapoint_id,
                   avg(value) AS avg_value, min(value) AS min_value, max(value) AS max_value,
                   count(*) AS n
            FROM measurement
            WHERE quality = 0
            GROUP BY bucket, plant_id, datapoint_id
            WITH DATA
        """)
        op.execute("""
            SELECT add_continuous_aggregate_policy('measurement_1h',
                start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 hour',
                schedule_interval => INTERVAL '30 minutes')
        """)

    # Row-level security on relational tables.
    op.execute("ALTER TABLE tenant ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON tenant USING (id = current_tenant())")
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
        )

    # Compressed hypertables cannot have RLS: expose them only through filtering views.
    op.execute("""
        CREATE VIEW measurement_visible WITH (security_barrier) AS
        SELECT m.ts, m.plant_id, m.datapoint_id, m.value, m.quality
        FROM measurement m
        JOIN plant p ON p.id = m.plant_id
        WHERE p.tenant_id = current_tenant()
    """)
    op.execute("""
        CREATE VIEW measurement_1h_visible WITH (security_barrier) AS
        SELECT c.bucket, c.plant_id, c.datapoint_id, c.avg_value, c.min_value, c.max_value, c.n
        FROM measurement_1h c
        JOIN plant p ON p.id = c.plant_id
        WHERE p.tenant_id = current_tenant()
    """)

    # The API role: no login of its own; the API switches to it per transaction.
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'heatbeat_api') THEN
                CREATE ROLE heatbeat_api NOLOGIN;
            END IF;
        END $$
    """)
    op.execute("GRANT heatbeat_api TO CURRENT_USER")
    op.execute("GRANT USAGE ON SCHEMA public TO heatbeat_api")
    op.execute("GRANT SELECT ON tenant, site, gateway, plant, datapoint TO heatbeat_api")
    op.execute("GRANT SELECT ON measurement_visible, measurement_1h_visible TO heatbeat_api")
    # Column-level grant: the API can never read password hashes.
    op.execute("GRANT SELECT (id, tenant_id, email, role, created_at) ON app_user TO heatbeat_api")


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM heatbeat_api")
    op.execute("REVOKE USAGE ON SCHEMA public FROM heatbeat_api")
    op.execute("DROP VIEW IF EXISTS measurement_1h_visible")
    op.execute("DROP VIEW IF EXISTS measurement_visible")
    op.execute("ALTER TABLE plant DROP CONSTRAINT IF EXISTS plant_gateway_fk")
    op.execute("DROP INDEX IF EXISTS plant_tenant_idx")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON plant")
    op.execute("ALTER TABLE plant DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plant DROP COLUMN tenant_id, DROP COLUMN site_id, DROP COLUMN plant_type, DROP COLUMN name")
    op.execute("DROP TABLE IF EXISTS gateway, site, app_user, tenant")
    op.execute("DROP FUNCTION IF EXISTS current_tenant()")
    # The role heatbeat_api is cluster-wide (shared with other databases) and is kept.
