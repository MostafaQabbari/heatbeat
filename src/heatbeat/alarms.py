"""Alarm state machine for a "limit with duration" rule.

What this file does: decides, sample by sample, whether an alarm is NORMAL,
PENDING (condition just started) or FIRING (condition held long enough). It is
a pure function: same inputs, same output, no database, no clock. Rules:
- fire only after the condition held for `delay` (no alarm on a single spike),
- clear only when the value is back past `limit -/+ hysteresis` (no flapping),
- ignore late samples older than the last one seen (decide by event time, S09).
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class AlarmState(StrEnum):
    NORMAL = "normal"
    PENDING = "pending"
    FIRING = "firing"


@dataclass(frozen=True)
class ThresholdRule:
    limit: float
    delay: timedelta
    hysteresis: float = 0.0
    above: bool = True  # True: alarm when value > limit; False: when value < limit

    def breached(self, value: float) -> bool:
        return value > self.limit if self.above else value < self.limit

    def cleared(self, value: float) -> bool:
        if self.above:
            return value < self.limit - self.hysteresis
        return value > self.limit + self.hysteresis


@dataclass(frozen=True)
class AlarmStatus:
    state: AlarmState = AlarmState.NORMAL
    since: datetime | None = None  # when the condition started
    last_ts: datetime | None = None  # newest sample seen


def step(rule: ThresholdRule, status: AlarmStatus, value: float, ts: datetime) -> AlarmStatus:
    """Return the new status after one sample (value measured at time ts)."""
    if status.last_ts is not None and ts <= status.last_ts:
        return status  # late or duplicate sample: never changes the decision

    status = replace(status, last_ts=ts)

    if status.state is AlarmState.NORMAL:
        return replace(status, state=AlarmState.PENDING, since=ts) if rule.breached(value) else status

    if status.state is AlarmState.PENDING:
        if not rule.breached(value):
            return replace(status, state=AlarmState.NORMAL, since=None)
        assert status.since is not None
        if ts - status.since >= rule.delay:
            return replace(status, state=AlarmState.FIRING)
        return status

    # FIRING
    if rule.cleared(value):
        return replace(status, state=AlarmState.NORMAL, since=None)
    return status
