"""Business rules for requesting a parameter change (used by commands and recommendations).

What this file does: one place that turns "please set X to Y" into a command:
the value must be writable and in range, the caller must have seen the current
version (optimistic concurrency, S16), only one change per parameter may be
open at a time, and big or risky changes wait for a second person.
"""

from datetime import UTC, datetime, timedelta

import psycopg

from heatbeat.api.problems import ApiProblem
from heatbeat.api.schemas import Command
from heatbeat.api.stores import CommandApiStore
from heatbeat.auth import User
from heatbeat.commands import CommandState, check_value, needs_approval
from heatbeat.config import Settings

APPROVAL_WINDOW = timedelta(hours=24)


def request_change(
    store: CommandApiStore,
    settings: Settings,
    user: User,
    plant_key: str,
    datapoint: str,
    value: float,
    expected_version: int,
    source: str = "portal",
    recommendation_id: int | None = None,
    pre_approved: bool = False,
) -> Command:
    plant = store.plant(plant_key)
    if plant is None:
        raise ApiProblem(404, "Plant not found", f"No plant with key {plant_key!r}")
    dp = store.datapoint(datapoint)
    if dp is None:
        raise ApiProblem(422, "Unknown datapoint", datapoint)
    problem = check_value(datapoint, dp["writable"], value, dp["min_value"], dp["max_value"])
    if problem:
        raise ApiProblem(422, "Value not allowed", problem)

    current, version = store.parameter_state(plant["id"], dp["id"])
    if expected_version != version:
        raise ApiProblem(
            409,
            "Parameter changed in the meantime",
            f"You saw version {expected_version}, the current version is {version}. Reload and try again.",
        )

    approval = not pre_approved and needs_approval(datapoint, current, value, settings.approval_threshold)
    now = datetime.now(UTC)
    try:
        return store.insert(
            {
                "tenant_id": user.tenant_id,
                "plant_id": plant["id"],
                "datapoint_id": dp["id"],
                "value": value,
                "previous": current,
                "version": version,
                "state": (CommandState.PENDING_APPROVAL if approval else CommandState.REQUESTED).value,
                "needs_approval": approval,
                "source": source,
                "recommendation_id": recommendation_id,
                "user_id": user.id,
                "expires_at": now + (APPROVAL_WINDOW if approval else timedelta(seconds=settings.command_ttl_s)),
            }
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ApiProblem(
            409, "Another change is still open", f"{datapoint} already has an open change for this plant"
        ) from exc
