"""Optimiser recommendations: review, approve (becomes a command) or dismiss.

What this file does: shows what the optimiser suggests and why. Approving
does NOT write to the boiler directly: it creates a normal command, which goes
through the same range checks, expiry, gateway confirmation and audit trail.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends

from heatbeat.api.command_service import request_change
from heatbeat.api.deps import TenantDb, get_settings, rate_limited, require
from heatbeat.api.problems import ApiProblem
from heatbeat.api.schemas import Problem, Recommendation
from heatbeat.api.stores import CommandApiStore, RecommendationStore
from heatbeat.auth import Role, User
from heatbeat.config import Settings

router = APIRouter(prefix="/v1", tags=["recommendations"], dependencies=[Depends(rate_limited)])
States = Literal["suggested", "approved", "applied", "measured", "dismissed"]


@router.get("/recommendations", response_model=list[Recommendation])
def list_recommendations(conn: TenantDb, state: States | None = None, plant: str | None = None) -> list[Recommendation]:
    return RecommendationStore(conn).list(state, plant)


def _decidable(store: RecommendationStore, rec_id: int) -> dict[str, object]:
    row = store.lock(rec_id)
    if row is None:
        raise ApiProblem(404, "Recommendation not found")
    if row["state"] != "suggested":
        raise ApiProblem(409, "Already decided", f"The recommendation is '{row['state']}'")
    return row


@router.post("/recommendations/{rec_id}/approve", response_model=Recommendation, responses={409: {"model": Problem}})
def approve(
    rec_id: int,
    conn: TenantDb,
    user: Annotated[User, Depends(require(Role.ADMIN))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Recommendation:
    store = RecommendationStore(conn)
    row = _decidable(store, rec_id)
    commands = CommandApiStore(conn)
    plant = commands.plant(str(row["plant_key"]))
    assert plant is not None
    _, version = commands.parameter_state(plant["id"], int(row["datapoint_id"]))  # type: ignore[call-overload]
    command = request_change(
        commands,
        settings,
        user,
        str(row["plant_key"]),
        str(row["datapoint"]),
        float(row["proposed_value"]),  # type: ignore[arg-type]
        version,
        source="optimiser",
        recommendation_id=rec_id,
        pre_approved=True,  # an admin just approved it
    )
    store.decide(rec_id, "approved", user.id, command.id)
    found = store.get(rec_id)
    assert found is not None
    return found


@router.post("/recommendations/{rec_id}/dismiss", response_model=Recommendation, responses={409: {"model": Problem}})
def dismiss(rec_id: int, conn: TenantDb, user: Annotated[User, Depends(require(Role.OPERATOR))]) -> Recommendation:
    store = RecommendationStore(conn)
    _decidable(store, rec_id)
    store.decide(rec_id, "dismissed", user.id)
    found = store.get(rec_id)
    assert found is not None
    return found
