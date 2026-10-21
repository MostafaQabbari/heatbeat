"""Remote parameter changes: parameters with versions, commands, audit trail.

What this file does:
- `parameter`: last confirmed value per plant and writable datapoint, with a
  version number for optimistic concurrency (two people, one parameter, S16)
- `command`: one requested change and its state machine; at most ONE open
  command per plant and parameter (partial unique index); the approver must
  be a different person than the requester
- `parameter_audit`: append-only history. A trigger refuses UPDATE and
  DELETE for everyone, and the API role may only read it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

OPEN_STATES = "('pending_approval', 'requested', 'sent')"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE parameter (
            tenant_id    integer  NOT NULL REFERENCES tenant (id),
            plant_id     integer  NOT NULL REFERENCES plant (id),
            datapoint_id smallint NOT NULL REFERENCES datapoint (id),
            value        double precision,
            version      integer NOT NULL DEFAULT 0,
            updated_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (plant_id, datapoint_id)
        )
    """)
    op.execute("""
        CREATE TABLE command (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         integer  NOT NULL REFERENCES tenant (id),
            plant_id          integer  NOT NULL REFERENCES plant (id),
            datapoint_id      smallint NOT NULL REFERENCES datapoint (id),
            requested_value   double precision NOT NULL,
            previous_value    double precision,
            expected_version  integer NOT NULL,
            state             text NOT NULL CHECK (state IN
                ('pending_approval', 'requested', 'sent', 'applied', 'rejected', 'expired', 'cancelled')),
            needs_approval    boolean NOT NULL DEFAULT false,
            source            text NOT NULL DEFAULT 'portal' CHECK (source IN ('portal', 'optimiser')),
            recommendation_id bigint,
            requested_by      integer REFERENCES app_user (id),
            approved_by       integer REFERENCES app_user (id),
            reason            text,
            applied_value     double precision,
            created_at        timestamptz NOT NULL DEFAULT now(),
            approved_at       timestamptz,
            sent_at           timestamptz,
            expires_at        timestamptz NOT NULL,
            result_at         timestamptz,
            CHECK (approved_by IS NULL OR approved_by IS DISTINCT FROM requested_by)
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX command_one_open_per_parameter ON command (plant_id, datapoint_id) "
        f"WHERE state IN {OPEN_STATES}"
    )
    op.execute("CREATE INDEX command_due_idx ON command (created_at) WHERE state = 'requested'")
    op.execute("CREATE INDEX command_plant_idx ON command (plant_id, created_at DESC)")

    op.execute("""
        CREATE TABLE parameter_audit (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id     integer  NOT NULL REFERENCES tenant (id),
            plant_id      integer  NOT NULL REFERENCES plant (id),
            datapoint_id  smallint NOT NULL REFERENCES datapoint (id),
            old_value     double precision,
            new_value     double precision NOT NULL,
            source        text NOT NULL,
            actor_user_id integer REFERENCES app_user (id),
            approved_by   integer REFERENCES app_user (id),
            command_id    uuid REFERENCES command (id),
            at            timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX audit_plant_idx ON parameter_audit (plant_id, at DESC)")
    op.execute("""
        CREATE FUNCTION audit_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'parameter_audit is append-only';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER parameter_audit_no_edit BEFORE UPDATE OR DELETE ON parameter_audit
        FOR EACH ROW EXECUTE FUNCTION audit_is_append_only()
    """)

    for table in ("parameter", "command", "parameter_audit"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
        )
    op.execute("GRANT SELECT ON parameter, command, parameter_audit TO heatbeat_api")
    op.execute("GRANT INSERT ON command TO heatbeat_api")
    op.execute("GRANT UPDATE (state, approved_by, approved_at, expires_at, reason) ON command TO heatbeat_api")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS parameter_audit, command, parameter")
    op.execute("DROP FUNCTION IF EXISTS audit_is_append_only()")
