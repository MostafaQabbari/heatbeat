"""Alarms and alarm rules.

What this file does: list current alarms (filter by state, urgency, plant),
acknowledge one ("I am on it"), read the alarm history, and manage rules.
Operators acknowledge; only admins create or change rules.
"""

from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import AwareDatetime

from heatbeat.api.deps import CurrentUser, TenantDb, rate_limited, require
from heatbeat.api.problems import ApiProblem
from heatbeat.api.schemas import Alarm, AlarmEvent, AlarmRule, AlarmRuleCreate, AlarmRuleUpdate, Problem
from heatbeat.api.stores import AlarmStore
from heatbeat.auth import Role, User

router = APIRouter(prefix="/v1", tags=["alarms"], dependencies=[Depends(rate_limited)])


def get_store(conn: TenantDb) -> AlarmStore:
    return AlarmStore(conn)


Store = Annotated[AlarmStore, Depends(get_store)]


@router.get("/alarms", response_model=list[Alarm])
def list_alarms(
    store: Store,
    state: Literal["normal", "pending", "firing"] | None = "firing",
    urgency: Literal["low", "medium", "high", "critical"] | None = None,
    plant: str | None = None,
) -> list[Alarm]:
    """Current alarms. Default: only the firing ones."""
    return store.alarms(state, urgency, plant)


@router.post("/alarms/{alarm_id}/acknowledge", response_model=Alarm, responses={404: {"model": Problem}})
def acknowledge(alarm_id: int, store: Store, user: Annotated[User, Depends(require(Role.OPERATOR))]) -> Alarm:
    alarm = store.acknowledge(alarm_id, user.id)
    if alarm is None:
        raise ApiProblem(404, "Alarm not found")
    return alarm


@router.get("/alarm-events", response_model=list[AlarmEvent])
def alarm_events(
    store: Store,
    start: Annotated[AwareDatetime, Query(alias="from")],
    end: Annotated[AwareDatetime, Query(alias="to")],
    plant: str | None = None,
) -> list[AlarmEvent]:
    if end <= start or end - start > timedelta(days=366):
        raise ApiProblem(422, "Invalid time range", "'to' must be after 'from', at most 366 days")
    return store.events(start, end, plant)


@router.get("/alarm-rules", response_model=list[AlarmRule])
def list_rules(store: Store, _user: CurrentUser) -> list[AlarmRule]:
    return store.rules()


@router.post("/alarm-rules", response_model=AlarmRule, status_code=status.HTTP_201_CREATED)
def create_rule(body: AlarmRuleCreate, store: Store, user: Annotated[User, Depends(require(Role.ADMIN))]) -> AlarmRule:
    return store.create_rule(user.tenant_id, body)


@router.patch("/alarm-rules/{rule_id}", response_model=AlarmRule, responses={404: {"model": Problem}})
def update_rule(
    rule_id: int, body: AlarmRuleUpdate, store: Store, _user: Annotated[User, Depends(require(Role.ADMIN))]
) -> AlarmRule:
    rule = store.update_rule(rule_id, body)
    if rule is None:
        raise ApiProblem(404, "Rule not found")
    return rule
