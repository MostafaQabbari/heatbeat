"""Remote parameter changes: request, approve (second person), cancel, history.

What this file does: the HTTP side of the command pipeline. The bridge then
publishes the command to the gateway and records the result; this module only
creates and manages requests. Roles: operators request and cancel, admins
approve. Nobody can approve their own request (four-eyes principle).
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from heatbeat.api.command_service import request_change
from heatbeat.api.deps import TenantDb, get_settings, rate_limited, require
from heatbeat.api.problems import ApiProblem
from heatbeat.api.schemas import AuditEntry, Command, CommandCreate, Problem
from heatbeat.api.stores import CommandApiStore
from heatbeat.auth import Role, User
from heatbeat.commands import CommandState
from heatbeat.config import Settings

router = APIRouter(prefix="/v1", tags=["commands"], dependencies=[Depends(rate_limited)])
ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": Problem},
    409: {"model": Problem},
    422: {"model": Problem},
    403: {"model": Problem},
}


def get_store(conn: TenantDb) -> CommandApiStore:
    return CommandApiStore(conn)


Store = Annotated[CommandApiStore, Depends(get_store)]
Operator = Annotated[User, Depends(require(Role.OPERATOR))]
Admin = Annotated[User, Depends(require(Role.ADMIN))]


@router.post(
    "/plants/{plant_key}/commands",
    response_model=Command,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def create_command(
    plant_key: str,
    body: CommandCreate,
    store: Store,
    user: Operator,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Command:
    """Request a parameter change. Big or risky changes wait for a second person (state pending_approval)."""
    return request_change(store, settings, user, plant_key, body.datapoint, body.value, body.expected_version)


@router.post("/commands/{command_id}/approve", response_model=Command, responses=ERRORS)
def approve_command(
    command_id: str, store: Store, user: Admin, settings: Annotated[Settings, Depends(get_settings)]
) -> Command:
    row = store.lock(command_id)
    if row is None:
        raise ApiProblem(404, "Command not found")
    if row["state"] != CommandState.PENDING_APPROVAL:
        raise ApiProblem(409, "Nothing to approve", f"The command is '{row['state']}'")
    if row["requested_by"] == user.id:
        raise ApiProblem(403, "Second person required", "You cannot approve your own request")
    store.set_state(
        command_id,
        CommandState.REQUESTED.value,
        approved_by=user.id,
        approved_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.command_ttl_s),
    )
    found = store.get(command_id)
    assert found is not None
    return found


@router.post("/commands/{command_id}/cancel", response_model=Command, responses=ERRORS)
def cancel_command(command_id: str, store: Store, _user: Operator) -> Command:
    row = store.lock(command_id)
    if row is None:
        raise ApiProblem(404, "Command not found")
    if row["state"] not in (CommandState.PENDING_APPROVAL, CommandState.REQUESTED):
        raise ApiProblem(409, "Too late to cancel", f"The command is already '{row['state']}'")
    store.set_state(command_id, CommandState.CANCELLED.value, reason="cancelled by a user")
    found = store.get(command_id)
    assert found is not None
    return found


@router.get("/commands/{command_id}", response_model=Command, responses=ERRORS)
def get_command(command_id: str, store: Store) -> Command:
    found = store.get(command_id)
    if found is None:
        raise ApiProblem(404, "Command not found")
    return found


@router.get("/plants/{plant_key}/commands", response_model=list[Command])
def plant_commands(plant_key: str, store: Store, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[Command]:
    return store.for_plant(plant_key, limit)


@router.get("/plants/{plant_key}/parameter-history", response_model=list[AuditEntry])
def parameter_history(
    plant_key: str, store: Store, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[AuditEntry]:
    """Append-only audit trail: who changed what, when, approved by whom."""
    return store.audit(plant_key, limit)
