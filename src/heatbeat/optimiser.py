"""Optimisers ("autopilot"): pure functions that turn data into recommendations.

What this file does:
1. Heating curve: if the rooms were clearly warmer than needed over the last
   days, propose lowering the curve level in a SMALL step (max 3 K). Observe,
   then repeat. Never jumps, never leaves the allowed range.
2. Summer shutdown / restart: if the weather forecast stays warm for the next
   days, propose switching space heating off; when it turns cold, back on.
Missing inputs give NO recommendation and a clear reason (S19). Nothing is
applied automatically: a person approves, then it goes through the normal
command pipeline (range checks, expiry, audit).
"""

from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median

VERSION = "2026.10.1"  # stored with every run, so results can be reproduced


@dataclass(frozen=True)
class Proposal:
    kind: str  # heating_curve | summer_shutdown | heating_restart
    datapoint: str
    current_value: float | None
    proposed_value: float
    title: str
    rationale: str


@dataclass(frozen=True)
class Skipped:
    reason: str


@dataclass(frozen=True)
class CurveSettings:
    target_room: float = 21.0  # degC
    tolerance: float = 1.0  # do nothing within target + tolerance
    max_step: float = 3.0  # K per recommendation
    min_level: float = 15.0  # allowed range of heating_curve_level
    min_hours: int = 48  # need at least this much data


DEFAULT_CURVE = CurveSettings()


def heating_curve(
    room_hourly: list[float], current_level: float | None, s: CurveSettings = DEFAULT_CURVE
) -> Proposal | Skipped:
    """`room_hourly`: hourly mean room temperatures of the last days."""
    if current_level is None:
        return Skipped("current heating-curve level unknown")
    if len(room_hourly) < s.min_hours:
        return Skipped(f"not enough room-temperature data ({len(room_hourly)} of {s.min_hours} hours)")
    typical = median(room_hourly)  # median: robust against a sunny afternoon
    excess = typical - s.target_room
    if excess <= s.tolerance:
        return Skipped(f"rooms at {typical:.1f} degC, within target")
    step = min(s.max_step, round(excess, 0))
    proposed = max(s.min_level, current_level - step)
    if proposed >= current_level:
        return Skipped("already at the lowest allowed level")
    return Proposal(
        kind="heating_curve",
        datapoint="heating_curve_level",
        current_value=current_level,
        proposed_value=proposed,
        title=f"Lower heating curve level from {current_level:.0f} to {proposed:.0f} degC",
        rationale=(
            f"Rooms were typically {typical:.1f} degC over the last {len(room_hourly)} hours, "
            f"{excess:.1f} K above the {s.target_room:.0f} degC target. A lower flow temperature "
            f"loses less heat and lets condensing boilers and heat pumps work more efficiently. "
            f"Small step of {step:.0f} K; the effect is measured afterwards, not promised."
        ),
    )


@dataclass(frozen=True)
class SeasonSettings:
    off_mean: float = 15.0  # forecast mean above this ...
    off_min: float = 10.0  # ... and minimum above this -> heating off
    on_mean: float = 12.0  # forecast mean below this ...
    on_min: float = 5.0  # ... or minimum below this -> heating on
    min_hours: int = 48  # forecast must cover at least 2 days


DEFAULT_SEASON = SeasonSettings()


def season_switch(
    forecast: list[tuple[datetime, float]],
    heating_enabled: float | None,
    s: SeasonSettings = DEFAULT_SEASON,
) -> Proposal | Skipped:
    """Summer shutdown or restart from the forecast of the next days."""
    if heating_enabled is None:
        return Skipped("current heating mode unknown")
    if len(forecast) < s.min_hours:
        return Skipped(f"forecast covers only {len(forecast)} hours (need {s.min_hours})")
    temps = [t for _, t in forecast]
    avg, low = mean(temps), min(temps)
    days = len(forecast) // 24
    if heating_enabled >= 0.5 and avg > s.off_mean and low > s.off_min:
        return Proposal(
            kind="summer_shutdown",
            datapoint="heating_enabled",
            current_value=heating_enabled,
            proposed_value=0.0,
            title="Switch space heating off (summer mode)",
            rationale=(
                f"Forecast for the next {days} days: mean {avg:.1f} degC, minimum {low:.1f} degC. "
                "Heating is not needed; hot water stays on."
            ),
        )
    if heating_enabled < 0.5 and (avg < s.on_mean or low < s.on_min):
        return Proposal(
            kind="heating_restart",
            datapoint="heating_enabled",
            current_value=heating_enabled,
            proposed_value=1.0,
            title="Switch space heating back on",
            rationale=f"Forecast for the next {days} days: mean {avg:.1f} degC, minimum {low:.1f} degC.",
        )
    return Skipped(f"no change needed (forecast mean {avg:.1f} degC, min {low:.1f} degC)")
