"""Alarm evaluation against the database (runs every minute in the Celery worker).

What this file does: for every (rule, plant) pair it
1. locks that alarm row with FOR UPDATE SKIP LOCKED, so two workers never
   evaluate the same alarm at once (a paused worker that wakes up late simply
   finds the row taken, scenario S18),
2. reads only the samples that arrived since the last evaluation, up to
   "now minus 60 s" (a watermark: data still in flight gets a chance to land),
3. runs the pure rule engine (alarm_rules.py),
4. writes the new state, the history event AND the notification outbox rows in
   ONE transaction (transactional outbox, S14).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from heatbeat.alarm_rules import RuleKind, RuleSpec, Sample, Transition, Urgency, derive_signal, evaluate
from heatbeat.alarms import AlarmState, AlarmStatus
from heatbeat.config import Settings

log = logging.getLogger("heatbeat.alarms")

SETTLE = timedelta(seconds=60)
INITIAL_LOOKBACK = timedelta(hours=3)

URGENCY_CHANNELS: dict[Urgency, tuple[str, ...]] = {
    Urgency.LOW: (),
    Urgency.MEDIUM: ("email",),
    Urgency.HIGH: ("email", "webhook"),
    Urgency.CRITICAL: ("email", "webhook", "sms"),
}

ALARM_SQL = """
    SELECT a.id, a.tenant_id, a.plant_id, a.state, a.since, a.last_ts, a.last_value,
           r.id AS rule_id, r.name, r.kind, r.above, r.threshold, r.delay_s, r.hysteresis,
           r.window_s, r.urgency, r.plant_type, r.datapoint_id, r.second_datapoint_id,
           d1.key AS dp, d2.key AS dp2,
           p.key AS plant_key, p.last_seen_at
    FROM alarm a
    JOIN alarm_rule r ON r.id = a.rule_id
    JOIN plant p      ON p.id = a.plant_id
    LEFT JOIN datapoint d1 ON d1.id = r.datapoint_id
    LEFT JOIN datapoint d2 ON d2.id = r.second_datapoint_id
    WHERE a.id = %s AND r.enabled
    FOR UPDATE OF a SKIP LOCKED
"""


@dataclass
class EngineStats:
    evaluated: int = 0
    skipped_locked: int = 0
    fired: int = 0
    cleared: int = 0


def to_spec(row: dict[str, Any]) -> RuleSpec:
    return RuleSpec(
        name=row["name"],
        kind=RuleKind(row["kind"]),
        datapoint=row["dp"],
        second_datapoint=row["dp2"],
        limit=row["threshold"],
        above=row["above"],
        delay=timedelta(seconds=row["delay_s"]),
        hysteresis=row["hysteresis"],
        window=timedelta(seconds=row["window_s"]) if row["window_s"] else None,
        plant_type=row["plant_type"],
        urgency=Urgency(row["urgency"]),
    )


def _samples(conn: psycopg.Connection, plant_id: int, dp_id: int, after: datetime, upto: datetime) -> list[Sample]:
    rows = conn.execute(
        """
        SELECT ts, value FROM measurement
        WHERE plant_id = %s AND datapoint_id = %s AND quality = 0 AND ts > %s AND ts <= %s
        ORDER BY ts
        """,
        (plant_id, dp_id, after, upto),
    ).fetchall()
    return [(ts, value) for ts, value in rows]


def _signal(
    conn: psycopg.Connection, row: dict[str, Any], spec: RuleSpec, status: AlarmStatus, now: datetime
) -> list[Sample]:
    if spec.kind is RuleKind.ABSENCE:
        return derive_signal(spec, [], now=now, last_seen=row["last_seen_at"])
    upto = now - SETTLE
    new_after = status.last_ts or (upto - INITIAL_LOOKBACK)
    fetch_after = new_after - spec.lookback  # windowed rules need some older history
    a = _samples(conn, row["plant_id"], row["datapoint_id"], fetch_after, upto)
    b = (
        _samples(conn, row["plant_id"], row["second_datapoint_id"], fetch_after, upto)
        if spec.second_datapoint
        else None
    )
    return [s for s in derive_signal(spec, a, b) if s[0] > new_after]


def _enqueue(
    conn: psycopg.Connection,
    settings: Settings,
    row: dict[str, Any],
    spec: RuleSpec,
    event_id: int,
    t: Transition,
) -> None:
    recipients = {"email": settings.alarm_email_to, "webhook": settings.webhook_url, "sms": "+49 000 0000000"}
    verb = "FIRED" if t.kind == "fired" else "cleared"
    subject = f"[{spec.urgency.upper()}] {spec.name}: {verb} at plant {row['plant_key']}"
    body = (
        f"Plant: {row['plant_key']}\nRule: {spec.name}\nEvent: {t.kind}\n"
        f"Time (device clock, UTC): {t.ts.isoformat()}\nValue: {t.value:.2f} (limit {spec.limit})\n"
    )
    for channel in URGENCY_CHANNELS[spec.urgency]:
        recipient = recipients[channel]
        if not recipient:
            continue  # channel not configured (e.g. no webhook URL)
        conn.execute(
            """
            INSERT INTO notification_outbox
                (tenant_id, alarm_id, alarm_event_id, channel, recipient, subject, body, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                row["tenant_id"],
                row["id"],
                event_id,
                channel,
                recipient,
                subject,
                body,
                f"alarm-event-{event_id}-{channel}",
            ),
        )


def evaluate_one(conn: psycopg.Connection, settings: Settings, alarm_id: int, now: datetime) -> list[Transition] | None:
    """Evaluate one alarm in its own transaction. None if another worker holds it."""
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(ALARM_SQL, (alarm_id,)).fetchone()
        if row is None:
            return None
        spec = to_spec(row)
        status = AlarmStatus(AlarmState(row["state"]), row["since"], row["last_ts"])
        signal = _signal(conn, row, spec, status, now)
        new_status, transitions = evaluate(spec, status, signal)

        opened_at = None
        for t in transitions:
            event_id = conn.execute(
                """
                INSERT INTO alarm_event (tenant_id, alarm_id, rule_id, plant_id, kind, at, value)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (row["tenant_id"], row["id"], row["rule_id"], row["plant_id"], t.kind, t.ts, t.value),
            ).fetchone()[0]  # type: ignore[index]
            _enqueue(conn, settings, row, spec, event_id, t)
            opened_at = t.ts if t.kind == "fired" else None

        fired_now = any(t.kind == "fired" for t in transitions)
        conn.execute(
            """
            UPDATE alarm SET state = %s, since = %s, last_ts = %s, last_value = %s, updated_at = now(),
                   opened_at = CASE WHEN %s THEN %s WHEN %s = 'firing' THEN opened_at ELSE NULL END,
                   acknowledged_by = CASE WHEN %s THEN NULL ELSE acknowledged_by END,
                   acknowledged_at = CASE WHEN %s THEN NULL ELSE acknowledged_at END
            WHERE id = %s
            """,
            (
                new_status.state.value,
                new_status.since,
                new_status.last_ts,
                signal[-1][1] if signal else row["last_value"],
                fired_now,
                opened_at,
                new_status.state.value,
                fired_now,
                fired_now,
                row["id"],
            ),
        )
        return transitions


def evaluate_alarms(conn: psycopg.Connection, settings: Settings, now: datetime) -> EngineStats:
    """Evaluate every enabled rule for every matching plant. `conn` must be autocommit."""
    with conn.transaction():
        conn.execute("""
            INSERT INTO alarm (tenant_id, rule_id, plant_id)
            SELECT r.tenant_id, r.id, p.id
            FROM alarm_rule r JOIN plant p ON p.tenant_id = r.tenant_id
            WHERE r.enabled AND (r.plant_type IS NULL OR r.plant_type = p.plant_type)
            ON CONFLICT (rule_id, plant_id) DO NOTHING
        """)
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT a.id FROM alarm a JOIN alarm_rule r ON r.id = a.rule_id WHERE r.enabled ORDER BY a.id"
            ).fetchall()
        ]

    stats = EngineStats()
    for alarm_id in ids:
        transitions = evaluate_one(conn, settings, alarm_id, now)
        if transitions is None:
            stats.skipped_locked += 1
            continue
        stats.evaluated += 1
        stats.fired += sum(t.kind == "fired" for t in transitions)
        stats.cleared += sum(t.kind == "cleared" for t in transitions)
    if stats.fired or stats.cleared:
        log.info("alarms: %s", stats)
    return stats
