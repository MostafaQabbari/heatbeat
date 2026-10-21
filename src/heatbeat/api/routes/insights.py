"""Fault analysis: how often, how long, which faults are the most frequent.

What this file does: statistics over the alarm history for a period: count
per rule, mean and total duration, still open, and the plants with the most
faults. A fault "occurrence" is one fired -> cleared episode.
"""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from heatbeat.api.deps import TenantDb, rate_limited
from heatbeat.api.problems import ApiProblem
from heatbeat.api.schemas import FaultStats
from heatbeat.api.stores import AlarmStore

router = APIRouter(prefix="/v1", tags=["insights"], dependencies=[Depends(rate_limited)])


@router.get("/faults/stats", response_model=FaultStats)
def fault_stats(
    conn: TenantDb,
    start: Annotated[AwareDatetime, Query(alias="from")],
    end: Annotated[AwareDatetime, Query(alias="to")],
) -> FaultStats:
    if end <= start or end - start > timedelta(days=366):
        raise ApiProblem(422, "Invalid time range", "'to' must be after 'from', at most 366 days")
    return AlarmStore(conn).fault_stats(start, end)
