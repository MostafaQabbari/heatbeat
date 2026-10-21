"""SQL for alarms, fault statistics, commands, parameters and recommendations.

What this file does: the data access the routes need, one small class per
area. Like repository.py, it relies on row-level security for tenant
isolation: every query runs as `heatbeat_api` with the caller's tenant set.
"""

from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from heatbeat.api.schemas import (
    Alarm,
    AlarmEvent,
    AlarmRule,
    AlarmRuleCreate,
    AlarmRuleUpdate,
    AuditEntry,
    Command,
    FaultStat,
    FaultStats,
    Parameter,
    PlantFaults,
    Recommendation,
)


def _rows(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _row(conn: psycopg.Connection, sql: str, params: Any = None) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchone()


class AlarmStore:
    RULE_SQL = """
        SELECT r.id, r.name, r.kind, d1.key AS datapoint, d2.key AS second_datapoint, r.plant_type,
               r.above, r.threshold, r.delay_s, r.hysteresis, r.window_s, r.urgency, r.enabled
        FROM alarm_rule r
        LEFT JOIN datapoint d1 ON d1.id = r.datapoint_id
        LEFT JOIN datapoint d2 ON d2.id = r.second_datapoint_id
    """
    ALARM_SQL = """
        SELECT a.id, p.key AS plant_key, p.name AS plant_name, r.id AS rule_id, r.name AS rule_name,
               r.kind, r.urgency, a.state, a.since, a.opened_at, a.last_value, r.threshold,
               u.email AS acknowledged_by, a.acknowledged_at
        FROM alarm a
        JOIN alarm_rule r ON r.id = a.rule_id
        JOIN plant p ON p.id = a.plant_id
        LEFT JOIN app_user u ON u.id = a.acknowledged_by
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def rules(self) -> list[AlarmRule]:
        return [AlarmRule(**r) for r in _rows(self.conn, self.RULE_SQL + " ORDER BY r.id")]

    def create_rule(self, tenant_id: int, rule: AlarmRuleCreate) -> AlarmRule:
        row = _row(
            self.conn,
            """
            INSERT INTO alarm_rule (tenant_id, name, kind, datapoint_id, second_datapoint_id, plant_type,
                                    above, threshold, delay_s, hysteresis, window_s, urgency)
            VALUES (%(tenant)s, %(name)s, %(kind)s,
                    (SELECT id FROM datapoint WHERE key = %(dp)s), (SELECT id FROM datapoint WHERE key = %(dp2)s),
                    %(ptype)s, %(above)s, %(threshold)s, %(delay)s, %(hyst)s, %(window)s, %(urgency)s)
            RETURNING id
            """,
            {
                "tenant": tenant_id,
                "name": rule.name,
                "kind": rule.kind,
                "dp": rule.datapoint,
                "dp2": rule.second_datapoint,
                "ptype": rule.plant_type,
                "above": rule.above,
                "threshold": rule.threshold,
                "delay": rule.delay_s,
                "hyst": rule.hysteresis,
                "window": rule.window_s,
                "urgency": rule.urgency,
            },
        )
        assert row is not None
        created = _row(self.conn, self.RULE_SQL + " WHERE r.id = %s", (row["id"],))
        assert created is not None
        return AlarmRule(**created)

    def update_rule(self, rule_id: int, change: AlarmRuleUpdate) -> AlarmRule | None:
        fields = change.model_dump(exclude_none=True)
        if fields:
            # Column names are quoted as identifiers, values are parameters: no SQL injection possible.
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name)) for name in fields
            )
            query = sql.SQL("UPDATE alarm_rule SET {} WHERE id = %(id)s").format(assignments)
            self.conn.execute(query, {**fields, "id": rule_id})
        row = _row(self.conn, self.RULE_SQL + " WHERE r.id = %s", (rule_id,))
        return AlarmRule(**row) if row else None

    def alarms(self, state: str | None, urgency: str | None, plant: str | None) -> list[Alarm]:
        rows = _rows(
            self.conn,
            self.ALARM_SQL
            + """
            WHERE (%(state)s::text IS NULL OR a.state = %(state)s)
              AND (%(urgency)s::text IS NULL OR r.urgency = %(urgency)s)
              AND (%(plant)s::text IS NULL OR p.key = %(plant)s)
            ORDER BY a.opened_at DESC NULLS LAST, a.id
            LIMIT 500
            """,
            {"state": state, "urgency": urgency, "plant": plant},
        )
        return [Alarm(**r) for r in rows]

    def acknowledge(self, alarm_id: int, user_id: int) -> Alarm | None:
        self.conn.execute(
            "UPDATE alarm SET acknowledged_by = %s, acknowledged_at = now() WHERE id = %s AND state = 'firing'",
            (user_id, alarm_id),
        )
        row = _row(self.conn, self.ALARM_SQL + " WHERE a.id = %s", (alarm_id,))
        return Alarm(**row) if row else None

    def events(self, start: datetime, end: datetime, plant: str | None) -> list[AlarmEvent]:
        rows = _rows(
            self.conn,
            """
            SELECT e.id, p.key AS plant_key, r.name AS rule_name, r.urgency, e.kind, e.at, e.value
            FROM alarm_event e
            JOIN alarm_rule r ON r.id = e.rule_id
            JOIN plant p ON p.id = e.plant_id
            WHERE e.at >= %s AND e.at < %s AND (%s::text IS NULL OR p.key = %s)
            ORDER BY e.at DESC, e.id DESC
            LIMIT 1000
            """,
            (start, end, plant, plant),
        )
        return [AlarmEvent(**r) for r in rows]

    def fault_stats(self, start: datetime, end: datetime) -> FaultStats:
        occurrences = """
            WITH ev AS (
                SELECT e.rule_id, e.plant_id, e.kind, e.at,
                       lead(e.kind) OVER w AS next_kind, lead(e.at) OVER w AS next_at
                FROM alarm_event e
                WINDOW w AS (PARTITION BY e.alarm_id ORDER BY e.at, e.id)
            )
            SELECT rule_id, plant_id, at AS started,
                   CASE WHEN next_kind = 'cleared' THEN next_at END AS ended
            FROM ev
            WHERE kind = 'fired' AND at >= %(start)s AND at < %(end)s
        """
        params = {"start": start, "end": end}
        by_rule = _rows(
            self.conn,
            f"""
            WITH occ AS ({occurrences})
            SELECT r.name AS rule_name, r.urgency, count(*) AS occurrences,
                   count(*) FILTER (WHERE occ.ended IS NULL) AS open_now,
                   avg(extract(epoch FROM occ.ended - occ.started)) FILTER (WHERE occ.ended IS NOT NULL)
                       AS mean_duration_s,
                   coalesce(sum(extract(epoch FROM coalesce(occ.ended, least(now(), %(end)s)) - occ.started)), 0)
                       AS total_duration_s
            FROM occ JOIN alarm_rule r ON r.id = occ.rule_id
            GROUP BY r.name, r.urgency
            ORDER BY occurrences DESC, rule_name
            """,  # noqa: S608 (constant SQL fragment; values are parameters)
            params,
        )
        by_plant = _rows(
            self.conn,
            f"""
            WITH occ AS ({occurrences})
            SELECT p.key AS plant_key, count(*) AS occurrences,
                   coalesce(sum(extract(epoch FROM coalesce(occ.ended, least(now(), %(end)s)) - occ.started)), 0)
                       AS total_duration_s
            FROM occ JOIN plant p ON p.id = occ.plant_id
            GROUP BY p.key
            ORDER BY occurrences DESC, plant_key
            """,  # noqa: S608 (constant SQL fragment; values are parameters)
            params,
        )
        stats = [FaultStat(**r) for r in by_rule]
        return FaultStats(
            start=start,
            end=end,
            total_occurrences=sum(s.occurrences for s in stats),
            most_frequent=stats[0].rule_name if stats else None,
            by_rule=stats,
            by_plant=[PlantFaults(**r) for r in by_plant],
        )


class CommandApiStore:
    COMMAND_SQL = """
        SELECT c.id::text AS id, p.key AS plant_key, d.key AS datapoint, c.requested_value, c.previous_value,
               c.state, c.needs_approval, c.source, ru.email AS requested_by, au.email AS approved_by,
               c.reason, c.applied_value, c.created_at, c.sent_at, c.expires_at, c.result_at
        FROM command c
        JOIN plant p ON p.id = c.plant_id
        JOIN datapoint d ON d.id = c.datapoint_id
        LEFT JOIN app_user ru ON ru.id = c.requested_by
        LEFT JOIN app_user au ON au.id = c.approved_by
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def plant(self, key: str) -> dict[str, Any] | None:
        return _row(self.conn, "SELECT id, tenant_id FROM plant WHERE key = %s", (key,))

    def datapoint(self, key: str) -> dict[str, Any] | None:
        return _row(self.conn, "SELECT id, key, writable, min_value, max_value FROM datapoint WHERE key = %s", (key,))

    def parameter_state(self, plant_id: int, datapoint_id: int) -> tuple[float | None, int]:
        """Current value (confirmed, else last reported by the gateway) and version."""
        row = _row(
            self.conn,
            """
            SELECT coalesce(pa.value, (
                       SELECT m.value FROM measurement_visible m
                       WHERE m.plant_id = %(p)s AND m.datapoint_id = %(d)s AND m.ts > now() - INTERVAL '1 day'
                       ORDER BY m.ts DESC LIMIT 1)) AS value,
                   coalesce(pa.version, 0) AS version
            FROM (SELECT 1) one
            LEFT JOIN parameter pa ON pa.plant_id = %(p)s AND pa.datapoint_id = %(d)s
            """,
            {"p": plant_id, "d": datapoint_id},
        )
        assert row is not None
        return row["value"], row["version"]

    def parameters(self, plant_key: str) -> list[Parameter]:
        rows = _rows(
            self.conn,
            """
            SELECT d.key AS datapoint, d.unit, d.min_value, d.max_value,
                   coalesce(pa.value, latest.value) AS value, coalesce(pa.version, 0) AS version
            FROM plant p
            CROSS JOIN datapoint d
            LEFT JOIN parameter pa ON pa.plant_id = p.id AND pa.datapoint_id = d.id
            LEFT JOIN LATERAL (
                SELECT m.value FROM measurement_visible m
                WHERE m.plant_id = p.id AND m.datapoint_id = d.id AND m.ts > now() - INTERVAL '1 day'
                ORDER BY m.ts DESC LIMIT 1) latest ON true
            WHERE p.key = %s AND d.writable
            ORDER BY d.key
            """,
            (plant_key,),
        )
        return [Parameter(**r) for r in rows]

    def insert(self, values: dict[str, Any]) -> Command:
        row = _row(
            self.conn,
            """
            INSERT INTO command (tenant_id, plant_id, datapoint_id, requested_value, previous_value,
                                 expected_version, state, needs_approval, source, recommendation_id,
                                 requested_by, expires_at)
            VALUES (%(tenant_id)s, %(plant_id)s, %(datapoint_id)s, %(value)s, %(previous)s, %(version)s,
                    %(state)s, %(needs_approval)s, %(source)s, %(recommendation_id)s, %(user_id)s, %(expires_at)s)
            RETURNING id
            """,
            values,
        )
        assert row is not None
        found = self.get(str(row["id"]))
        assert found is not None
        return found

    def get(self, command_id: str) -> Command | None:
        row = _row(self.conn, self.COMMAND_SQL + " WHERE c.id::text = %s", (command_id,))
        return Command(**row) if row else None

    def lock(self, command_id: str) -> dict[str, Any] | None:
        return _row(
            self.conn,
            "SELECT id, state, requested_by FROM command WHERE id::text = %s FOR UPDATE",
            (command_id,),
        )

    def set_state(self, command_id: str, state: str, **extra: Any) -> None:
        values = {"state": state, **extra}
        sets = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name)) for name in values
        )
        query = sql.SQL("UPDATE command SET {} WHERE id::text = %(id)s").format(sets)
        self.conn.execute(query, {**values, "id": command_id})

    def for_plant(self, plant_key: str, limit: int) -> list[Command]:
        rows = _rows(
            self.conn,
            self.COMMAND_SQL + " WHERE p.key = %s ORDER BY c.created_at DESC LIMIT %s",
            (plant_key, limit),
        )
        return [Command(**r) for r in rows]

    def audit(self, plant_key: str, limit: int) -> list[AuditEntry]:
        rows = _rows(
            self.conn,
            """
            SELECT a.id, a.at, d.key AS datapoint, a.old_value, a.new_value, a.source,
                   u.email AS actor, v.email AS approved_by, a.command_id::text AS command_id
            FROM parameter_audit a
            JOIN plant p ON p.id = a.plant_id
            JOIN datapoint d ON d.id = a.datapoint_id
            LEFT JOIN app_user u ON u.id = a.actor_user_id
            LEFT JOIN app_user v ON v.id = a.approved_by
            WHERE p.key = %s
            ORDER BY a.at DESC, a.id DESC
            LIMIT %s
            """,
            (plant_key, limit),
        )
        return [AuditEntry(**r) for r in rows]


class RecommendationStore:
    SQL = """
        SELECT r.id, p.key AS plant_key, r.kind, r.title, r.rationale, d.key AS datapoint, r.current_value,
               r.proposed_value, r.state, r.created_at, u.email AS decided_by, r.decided_at,
               r.command_id::text AS command_id, r.measured_effect
        FROM recommendation r
        JOIN plant p ON p.id = r.plant_id
        JOIN datapoint d ON d.id = r.datapoint_id
        LEFT JOIN app_user u ON u.id = r.decided_by
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def list(self, state: str | None, plant: str | None) -> list[Recommendation]:
        rows = _rows(
            self.conn,
            self.SQL + " WHERE (%(state)s::text IS NULL OR r.state = %(state)s)"
            " AND (%(plant)s::text IS NULL OR p.key = %(plant)s)"
            " ORDER BY r.created_at DESC LIMIT 500",
            {"state": state, "plant": plant},
        )
        return [Recommendation(**r) for r in rows]

    def get(self, rec_id: int) -> Recommendation | None:
        row = _row(self.conn, self.SQL + " WHERE r.id = %s", (rec_id,))
        return Recommendation(**row) if row else None

    def lock(self, rec_id: int) -> dict[str, Any] | None:
        return _row(
            self.conn,
            "SELECT r.id, r.state, r.plant_id, r.datapoint_id, r.proposed_value, "
            "p.key AS plant_key, d.key AS datapoint "
            "FROM recommendation r JOIN plant p ON p.id = r.plant_id JOIN datapoint d ON d.id = r.datapoint_id "
            "WHERE r.id = %s FOR UPDATE OF r",
            (rec_id,),
        )

    def decide(self, rec_id: int, state: str, user_id: int, command_id: str | None = None) -> None:
        self.conn.execute(
            "UPDATE recommendation SET state = %s, decided_by = %s, decided_at = now(), command_id = %s::uuid "
            "WHERE id = %s",
            (state, user_id, command_id, rec_id),
        )
