"""Alarm rule types, turned into ONE kind of signal for ONE state machine.

What this file does: every rule type first derives a number over time
("signal"), then the same state machine (alarms.py) decides on it:
- threshold: the value itself (or its rolling mean over `window`)
- spread:    value A minus value B (for example flow minus return)
- rate:      0 -> 1 switches per hour in the last `window` (burner starts: short cycling)
- flatline:  max - min in the last `window` (a stuck sensor never changes)
- absence:   minutes since the plant last sent data (gateway offline)
All pure functions: no database, no clock, easy to test.
The default limits come from measuring healthy and faulty simulated plants.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from heatbeat.alarms import AlarmState, AlarmStatus, ThresholdRule, step

Sample = tuple[datetime, float]
HOUR = timedelta(hours=1)


class RuleKind(StrEnum):
    THRESHOLD = "threshold"
    SPREAD = "spread"
    RATE = "rate"
    FLATLINE = "flatline"
    ABSENCE = "absence"


class Urgency(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class RuleSpec:
    name: str
    kind: RuleKind
    datapoint: str | None
    limit: float
    above: bool
    delay: timedelta
    hysteresis: float = 0.0
    window: timedelta | None = None
    second_datapoint: str | None = None  # spread only
    plant_type: str | None = None  # None = every plant
    urgency: Urgency = Urgency.MEDIUM

    @property
    def state_rule(self) -> ThresholdRule:
        return ThresholdRule(limit=self.limit, delay=self.delay, hysteresis=self.hysteresis, above=self.above)

    @property
    def lookback(self) -> timedelta:
        """How much history before the new samples a signal needs."""
        return self.window or timedelta(0)


@dataclass(frozen=True)
class Transition:
    kind: str  # "fired" or "cleared"
    ts: datetime
    value: float


def _window_slices(samples: list[Sample], window: timedelta) -> list[tuple[datetime, list[float]]]:
    """For each sample, the values in (ts - window, ts]; only once a full window of history exists."""
    times = [t for t, _ in samples]
    out = []
    for i, (ts, _) in enumerate(samples):
        if ts - times[0] < window:
            continue
        lo = bisect_left(times, ts - window, hi=i)
        if times[lo] <= ts - window:
            lo += 1
        out.append((ts, [v for _, v in samples[lo : i + 1]]))
    return out


def derive_signal(
    rule: RuleSpec,
    a: list[Sample],
    b: list[Sample] | None = None,
    now: datetime | None = None,
    last_seen: datetime | None = None,
) -> list[Sample]:
    """Turn raw samples (sorted by time) into the signal the state machine judges."""
    if rule.kind is RuleKind.THRESHOLD:
        if rule.window is None:
            return list(a)
        return [(ts, sum(vs) / len(vs)) for ts, vs in _window_slices(a, rule.window)]

    if rule.kind is RuleKind.SPREAD:
        other = dict(b or [])
        return [(ts, v - other[ts]) for ts, v in a if ts in other]

    if rule.kind is RuleKind.RATE:
        window = rule.window or HOUR
        edges = [0.0] + [1.0 if prev < 0.5 <= cur else 0.0 for (_, prev), (_, cur) in pairwise(a)]
        marked = [(ts, e) for (ts, _), e in zip(a, edges, strict=True)]
        per_hour = HOUR / window
        return [(ts, sum(vs) * per_hour) for ts, vs in _window_slices(marked, window)]

    if rule.kind is RuleKind.FLATLINE:
        window = rule.window or HOUR
        return [(ts, max(vs) - min(vs)) for ts, vs in _window_slices(a, window) if len(vs) >= 3]

    # ABSENCE: judged at "now", from the last time the plant sent anything.
    if now is None or last_seen is None:
        return []
    return [(now, (now - last_seen).total_seconds() / 60)]


def evaluate(rule: RuleSpec, status: AlarmStatus, signal: list[Sample]) -> tuple[AlarmStatus, list[Transition]]:
    """Feed the signal into the state machine; report when the alarm fired or cleared."""
    transitions = []
    for ts, value in signal:
        before = status.state
        status = step(rule.state_rule, status, value, ts)
        if before is not AlarmState.FIRING and status.state is AlarmState.FIRING:
            transitions.append(Transition("fired", ts, value))
        elif before is AlarmState.FIRING and status.state is AlarmState.NORMAL:
            transitions.append(Transition("cleared", ts, value))
    return status, transitions


M = timedelta(minutes=1)

DEFAULT_RULES: list[RuleSpec] = [
    RuleSpec("No data from plant", RuleKind.ABSENCE, None, 10, True, 0 * M, urgency=Urgency.HIGH),
    RuleSpec(
        "Flow temperature too high",
        RuleKind.THRESHOLD,
        "flow_temp",
        85,
        True,
        10 * M,
        3,
        urgency=Urgency.HIGH,
    ),
    RuleSpec(
        "Rooms overheated (heating curve too high?)",
        RuleKind.THRESHOLD,
        "room_temp",
        24.5,
        True,
        120 * M,
        0.5,
    ),
    RuleSpec("Flow sensor stuck", RuleKind.FLATLINE, "flow_temp", 0.05, False, 0 * M, 0.2, window=30 * M),
    RuleSpec(
        "Short cycling (burner starts per hour)",
        RuleKind.RATE,
        "burner_state",
        13,
        True,
        15 * M,
        2,
        window=HOUR,
        plant_type="gas_boiler",
    ),
    RuleSpec(
        "Low spread flow/return (hydraulics)",
        RuleKind.SPREAD,
        "flow_temp",
        3.0,
        False,
        30 * M,
        0.5,
        second_datapoint="return_temp",
        plant_type="gas_boiler",
    ),
    RuleSpec(
        "Low spread flow/return (hydraulics)",
        RuleKind.SPREAD,
        "flow_temp",
        1.5,
        False,
        30 * M,
        0.3,
        second_datapoint="return_temp",
        plant_type="district_heating",
    ),
    RuleSpec(
        "Heat pump efficiency low (COP, 1 h average)",
        RuleKind.THRESHOLD,
        "cop",
        2.7,
        False,
        60 * M,
        0.2,
        window=HOUR,
        plant_type="heat_pump",
    ),
]
