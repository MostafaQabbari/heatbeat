"""Tests for the optimisers (src/heatbeat/optimiser.py), scenario S19."""

from datetime import UTC, datetime, timedelta

import pytest

from heatbeat.optimiser import Proposal, Skipped, heating_curve, season_switch

T0 = datetime(2026, 5, 1, tzinfo=UTC)


def forecast(temps: list[float]) -> list[tuple[datetime, float]]:
    return [(T0 + timedelta(hours=i), t) for i, t in enumerate(temps)]


def test_warm_rooms_lead_to_a_small_curve_reduction():
    result = heating_curve([24.5] * 72, current_level=40)

    assert isinstance(result, Proposal)
    assert result.proposed_value == 37  # 3.5 K too warm -> step capped at 3 K, never a jump
    assert "measured afterwards" in result.rationale  # no promised percentage


def test_comfortable_rooms_need_no_change():
    assert isinstance(heating_curve([21.4] * 72, current_level=40), Skipped)


def test_never_below_the_allowed_minimum():
    result = heating_curve([28.0] * 72, current_level=16)

    assert isinstance(result, Proposal)
    assert result.proposed_value == 15


def test_a_sunny_afternoon_does_not_trigger_it():
    """The median ignores a few hot hours."""
    rooms = [21.2] * 60 + [27.0] * 12

    assert isinstance(heating_curve(rooms, current_level=40), Skipped)


@pytest.mark.parametrize(("rooms", "level"), [([24.0] * 10, 40), ([24.0] * 72, None)])
def test_s19_missing_inputs_give_no_recommendation(rooms, level):
    result = heating_curve(rooms, current_level=level)

    assert isinstance(result, Skipped)


def test_warm_forecast_proposes_summer_mode():
    result = season_switch(forecast([18 + (i % 24) / 4 for i in range(72)]), heating_enabled=1.0)

    assert isinstance(result, Proposal)
    assert (result.kind, result.proposed_value) == ("summer_shutdown", 0.0)


def test_cold_forecast_switches_heating_back_on():
    result = season_switch(forecast([6.0] * 72), heating_enabled=0.0)

    assert isinstance(result, Proposal)
    assert (result.kind, result.proposed_value) == ("heating_restart", 1.0)


def test_one_cold_night_keeps_heating_on():
    temps = [18.0] * 72
    temps[30] = 7.0  # minimum below the "off" threshold

    assert isinstance(season_switch(forecast(temps), heating_enabled=1.0), Skipped)


def test_s19_forecast_missing_means_no_recommendation():
    """The weather service was down: better no advice than advice based on nothing."""
    result = season_switch(forecast([20.0] * 10), heating_enabled=1.0)

    assert isinstance(result, Skipped)
    assert "forecast covers only 10 hours" in result.reason
