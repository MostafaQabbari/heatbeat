"""Datapoint catalogue for all plant types, and writable parameters.

What this file does: adds the datapoints of heat pumps and district-heating
substations, and marks three parameters as writable (remote changes allowed)
with the range the cloud accepts. Plausible range = "a sensor could report
this"; allowed range = "we may set this".

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

NEW_DATAPOINTS = [
    # key, unit, description, plausible min, plausible max
    ("room_temp", "degC", "Room temperature (reference room)", -10, 40),
    ("burner_power_kw", "kW", "Burner power", 0, 500),
    ("compressor_state", "", "Compressor on (1) or off (0)", 0, 1),
    ("defrost_state", "", "Defrost running (1) or not (0)", 0, 1),
    ("heat_power_kw", "kW", "Heat output", 0, 500),
    ("electrical_power_kw", "kW", "Electrical input", 0, 200),
    ("cop", "", "Coefficient of performance (heat / electricity)", 0, 10),
    ("primary_supply_temp", "degC", "District heating network supply", 0, 150),
    ("primary_return_temp", "degC", "District heating network return", 0, 150),
    ("valve_position", "%", "Control valve opening", 0, 100),
]

WRITABLE = [
    # key, unit, description, plausible min/max, allowed min/max
    ("heating_curve_level", "degC", "Heating curve level (flow at 20 degC outside)", 0, 80, 15, 50),
    ("heating_curve_slope", "", "Heating curve slope", 0, 5, 0.2, 3.0),
    ("heating_enabled", "", "Space heating on (1) or off (0, summer mode)", 0, 1, 0, 1),
]


def upgrade() -> None:
    op.execute("""
        ALTER TABLE datapoint
            ADD COLUMN writable  boolean NOT NULL DEFAULT false,
            ADD COLUMN min_value double precision,
            ADD COLUMN max_value double precision,
            ADD CONSTRAINT writable_needs_range
                CHECK (NOT writable OR (min_value IS NOT NULL AND max_value IS NOT NULL
                                        AND min_value < max_value))
    """)
    for key, unit, desc, lo, hi in NEW_DATAPOINTS:
        op.execute(
            "INSERT INTO datapoint (key, unit, description, min_plausible, max_plausible) "
            f"VALUES ('{key}', '{unit}', '{desc}', {lo}, {hi})"
        )
    for key, unit, desc, lo, hi, wmin, wmax in WRITABLE:
        op.execute(
            "INSERT INTO datapoint (key, unit, description, min_plausible, max_plausible, "
            "writable, min_value, max_value) "
            f"VALUES ('{key}', '{unit}', '{desc}', {lo}, {hi}, true, {wmin}, {wmax})"
        )


def downgrade() -> None:
    keys = [k for k, *_ in NEW_DATAPOINTS] + [k for k, *_ in WRITABLE]
    in_list = ", ".join(f"'{k}'" for k in keys)
    # Deleting from compressed chunks decompresses them; TimescaleDB caps that at
    # 100,000 rows per transaction by default. 0 = no cap, for this transaction only.
    op.execute("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
    op.execute(f"DELETE FROM measurement WHERE datapoint_id IN (SELECT id FROM datapoint WHERE key IN ({in_list}))")
    op.execute(f"DELETE FROM datapoint WHERE key IN ({in_list})")
    op.execute("""
        ALTER TABLE datapoint
            DROP CONSTRAINT writable_needs_range,
            DROP COLUMN writable, DROP COLUMN min_value, DROP COLUMN max_value
    """)
