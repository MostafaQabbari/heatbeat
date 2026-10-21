"""Remote parameter changes: the command state machine and its database steps.

What this file does:
- pure rules: which state changes are allowed, when a second person must
  approve, how a gateway result changes a command (duplicates are ignored)
- `CommandStore`: the database steps used by the bridge and the worker:
  publish due commands, record results (and the audit row), expire old ones.

    pending_approval --approve--> requested --publish--> sent --result--> applied / rejected
            \\--cancel / expire       \\--cancel / expire     \\--expire--> expired --late result--> applied / rejected

A late "applied" after expiry is accepted: the gateway is the truth about what
happened to the boiler (it never applies an expired command itself).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import psycopg
from psycopg.rows import dict_row

from heatbeat.contract import CommandMessage, CommandResult

log = logging.getLogger("heatbeat.commands")


class CommandState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    REQUESTED = "requested"
    SENT = "sent"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


OPEN_STATES = {CommandState.PENDING_APPROVAL, CommandState.REQUESTED, CommandState.SENT}

ALLOWED: dict[CommandState, set[CommandState]] = {
    CommandState.PENDING_APPROVAL: {CommandState.REQUESTED, CommandState.CANCELLED, CommandState.EXPIRED},
    CommandState.REQUESTED: {CommandState.SENT, CommandState.CANCELLED, CommandState.EXPIRED},
    CommandState.SENT: {CommandState.APPLIED, CommandState.REJECTED, CommandState.EXPIRED},
    CommandState.EXPIRED: {CommandState.APPLIED, CommandState.REJECTED},
    CommandState.APPLIED: set(),
    CommandState.REJECTED: set(),
    CommandState.CANCELLED: set(),
}

ALWAYS_NEEDS_APPROVAL = {"heating_enabled"}  # switching heating off is never a small change


def can_move(current: CommandState, target: CommandState) -> bool:
    return target in ALLOWED[current]


def after_result(current: CommandState, result_status: str) -> CommandState | None:
    """New state after a gateway result, or None to ignore it (duplicate or too late)."""
    target = {
        "applied": CommandState.APPLIED,
        "rejected": CommandState.REJECTED,
        "expired": CommandState.EXPIRED,
    }[result_status]
    if current is CommandState.REQUESTED:
        current = CommandState.SENT  # the result can overtake our own "sent" bookkeeping
    return target if can_move(current, target) else None


def needs_approval(datapoint: str, old: float | None, new: float, threshold: float) -> bool:
    if datapoint in ALWAYS_NEEDS_APPROVAL:
        return True
    return old is None or abs(new - old) > threshold


def check_value(datapoint: str, writable: bool, value: float, lo: float | None, hi: float | None) -> str | None:
    """Return why the value is not allowed, or None."""
    if not writable or lo is None or hi is None:
        return f"{datapoint} cannot be changed remotely"
    if not lo <= value <= hi:
        return f"{value} is outside the allowed range {lo} to {hi}"
    return None


@dataclass
class ResultOutcome:
    accepted: bool
    state: CommandState | None = None


class CommandStore:
    """Database steps for commands. Runs as the system role (no tenant restriction)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn  # autocommit connection; each method uses its own transaction

    def publish_due(self, publish: Callable[[str, CommandMessage], bool], now: datetime, limit: int = 50) -> int:
        """Send 'requested' commands to their gateways. Returns how many were sent.

        `publish(gateway, message)` returns False if the broker is not reachable;
        the command then simply stays 'requested' and is tried again later.
        A command published twice is harmless: the gateway dedupes by command_id.
        """
        sent = 0
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT c.id, c.requested_value, c.expires_at, p.key AS plant, p.gateway, d.key AS dp
                FROM command c
                JOIN plant p ON p.id = c.plant_id
                JOIN datapoint d ON d.id = c.datapoint_id
                WHERE c.state = 'requested'
                ORDER BY c.created_at
                LIMIT %s
                FOR UPDATE OF c SKIP LOCKED
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                if row["expires_at"] <= now:
                    cur.execute(
                        "UPDATE command SET state = 'expired', result_at = %s, reason = %s WHERE id = %s",
                        (now, "expired before it could be sent", row["id"]),
                    )
                    continue
                message = CommandMessage(
                    command_id=str(row["id"]),
                    plant=row["plant"],
                    dp=row["dp"],
                    value=row["requested_value"],
                    expires_at=row["expires_at"],
                )
                if publish(row["gateway"], message):
                    cur.execute("UPDATE command SET state = 'sent', sent_at = %s WHERE id = %s", (now, row["id"]))
                    sent += 1
        return sent

    def record_result(self, result: CommandResult) -> ResultOutcome:
        """Apply a gateway result. Duplicate or foreign results are ignored."""
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """
                SELECT c.id, c.state, c.tenant_id, c.plant_id, c.datapoint_id, c.previous_value,
                       c.requested_by, c.approved_by, c.source, p.gateway
                FROM command c JOIN plant p ON p.id = c.plant_id
                WHERE c.id::text = %s
                FOR UPDATE OF c
                """,
                (result.command_id,),
            ).fetchone()
            if row is None or row["gateway"] != result.gateway:
                log.warning("ignoring result for unknown or foreign command %s", result.command_id)
                return ResultOutcome(False)
            new_state = after_result(CommandState(row["state"]), result.status)
            if new_state is None:
                return ResultOutcome(False, CommandState(row["state"]))  # duplicate result

            cur.execute(
                "UPDATE command SET state = %s, result_at = %s, applied_value = %s, reason = %s WHERE id = %s",
                (new_state.value, result.at, result.applied_value, result.reason, row["id"]),
            )
            # A recommendation that was turned into this command follows its outcome.
            cur.execute(
                """
                UPDATE recommendation SET
                    state = CASE WHEN %(applied)s THEN 'applied' ELSE 'dismissed' END,
                    applied_at = CASE WHEN %(applied)s THEN %(at)s ELSE applied_at END,
                    measured_effect = CASE WHEN %(applied)s THEN measured_effect
                                           ELSE 'command ' || %(state)s || ': ' || coalesce(%(reason)s, '') END
                WHERE command_id = %(id)s AND state = 'approved'
                """,
                {
                    "applied": new_state is CommandState.APPLIED,
                    "at": result.at,
                    "state": new_state.value,
                    "reason": result.reason,
                    "id": row["id"],
                },
            )
            if new_state is CommandState.APPLIED:
                cur.execute(
                    """
                    INSERT INTO parameter (tenant_id, plant_id, datapoint_id, value, version, updated_at)
                    VALUES (%s, %s, %s, %s, 1, %s)
                    ON CONFLICT (plant_id, datapoint_id)
                    DO UPDATE SET value = EXCLUDED.value, version = parameter.version + 1,
                                  updated_at = EXCLUDED.updated_at
                    """,
                    (row["tenant_id"], row["plant_id"], row["datapoint_id"], result.applied_value, result.at),
                )
                cur.execute(
                    """
                    INSERT INTO parameter_audit (tenant_id, plant_id, datapoint_id, old_value, new_value,
                                                 source, actor_user_id, approved_by, command_id, at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row["tenant_id"],
                        row["plant_id"],
                        row["datapoint_id"],
                        row["previous_value"],
                        result.applied_value,
                        row["source"],
                        row["requested_by"],
                        row["approved_by"],
                        row["id"],
                        result.at,
                    ),
                )
            return ResultOutcome(True, new_state)

    def expire(self, now: datetime) -> int:
        """Mark open commands past their expiry as expired. Returns how many."""
        with self.conn.transaction():
            expired = self.conn.execute(
                """
                UPDATE command SET state = 'expired', result_at = %s,
                       reason = coalesce(reason, 'no result from the gateway before the expiry')
                WHERE state IN ('pending_approval', 'requested', 'sent') AND expires_at <= %s
                RETURNING id
                """,
                (now, now),
            ).fetchall()
            count = len(expired)
            if expired:
                self.conn.execute(
                    "UPDATE recommendation SET state = 'dismissed', measured_effect = 'command expired' "
                    "WHERE command_id = ANY(%s) AND state = 'approved'",
                    ([r[0] for r in expired],),
                )
        if count:
            log.warning("%d command(s) expired without a result", count)
        return count
