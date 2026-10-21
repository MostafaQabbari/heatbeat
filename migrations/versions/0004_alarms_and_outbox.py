"""Alarm rules, alarm state, alarm history and the notification outbox.

What this file does: `alarm_rule` (what to watch), `alarm` (current state per
rule and plant), `alarm_event` (append-only history: fired / cleared) and
`notification_outbox` (messages to send). An alarm event and its outbox rows
are written in ONE transaction, so a crash can never lose a notification
(transactional outbox, DDIA ch. 12, p. 507-508).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE alarm_rule (
            id                  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id           integer NOT NULL REFERENCES tenant (id),
            name                text NOT NULL,
            kind                text NOT NULL CHECK (kind IN ('threshold', 'spread', 'rate', 'flatline', 'absence')),
            datapoint_id        smallint REFERENCES datapoint (id),
            second_datapoint_id smallint REFERENCES datapoint (id),
            plant_type          text,
            above               boolean NOT NULL,
            threshold           double precision NOT NULL,
            delay_s             integer NOT NULL DEFAULT 0 CHECK (delay_s >= 0),
            hysteresis          double precision NOT NULL DEFAULT 0 CHECK (hysteresis >= 0),
            window_s            integer CHECK (window_s > 0),
            urgency             text NOT NULL CHECK (urgency IN ('low', 'medium', 'high', 'critical')),
            enabled             boolean NOT NULL DEFAULT true,
            created_at          timestamptz NOT NULL DEFAULT now(),
            CHECK (kind = 'absence' OR datapoint_id IS NOT NULL),
            CHECK (kind <> 'spread' OR second_datapoint_id IS NOT NULL),
            UNIQUE NULLS NOT DISTINCT (tenant_id, name, plant_type)
        )
    """)
    op.execute("""
        CREATE TABLE alarm (
            id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id       integer NOT NULL REFERENCES tenant (id),
            rule_id         integer NOT NULL REFERENCES alarm_rule (id) ON DELETE CASCADE,
            plant_id        integer NOT NULL REFERENCES plant (id),
            state           text NOT NULL DEFAULT 'normal' CHECK (state IN ('normal', 'pending', 'firing')),
            since           timestamptz,
            last_ts         timestamptz,
            last_value      double precision,
            opened_at       timestamptz,
            acknowledged_by integer REFERENCES app_user (id),
            acknowledged_at timestamptz,
            updated_at      timestamptz NOT NULL DEFAULT now(),
            UNIQUE (rule_id, plant_id)
        )
    """)
    op.execute("CREATE INDEX alarm_firing_idx ON alarm (tenant_id, state) WHERE state = 'firing'")
    op.execute("""
        CREATE TABLE alarm_event (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id  integer NOT NULL REFERENCES tenant (id),
            alarm_id   integer NOT NULL REFERENCES alarm (id) ON DELETE CASCADE,
            rule_id    integer NOT NULL REFERENCES alarm_rule (id) ON DELETE CASCADE,
            plant_id   integer NOT NULL REFERENCES plant (id),
            kind       text NOT NULL CHECK (kind IN ('fired', 'cleared')),
            at         timestamptz NOT NULL,
            value      double precision,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX alarm_event_time_idx ON alarm_event (tenant_id, at)")
    op.execute("""
        CREATE TABLE notification_outbox (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id       integer NOT NULL REFERENCES tenant (id),
            alarm_id        integer NOT NULL REFERENCES alarm (id) ON DELETE CASCADE,
            alarm_event_id  bigint NOT NULL REFERENCES alarm_event (id) ON DELETE CASCADE,
            channel         text NOT NULL CHECK (channel IN ('email', 'webhook', 'sms')),
            recipient       text NOT NULL,
            subject         text NOT NULL,
            body            text NOT NULL,
            idempotency_key text NOT NULL UNIQUE,
            status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dead')),
            attempts        integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            last_error      text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            sent_at         timestamptz
        )
    """)
    op.execute("CREATE INDEX outbox_due_idx ON notification_outbox (next_attempt_at) WHERE status = 'pending'")

    for table in ("alarm_rule", "alarm", "alarm_event"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
        )
    op.execute("GRANT SELECT ON alarm_rule, alarm, alarm_event TO heatbeat_api")
    op.execute("GRANT INSERT, UPDATE (enabled, threshold, delay_s, hysteresis, urgency) ON alarm_rule TO heatbeat_api")
    op.execute("GRANT UPDATE (acknowledged_by, acknowledged_at) ON alarm TO heatbeat_api")
    # notification_outbox: deliberately no grant; only the workers (system role) use it.


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notification_outbox, alarm_event, alarm, alarm_rule")
