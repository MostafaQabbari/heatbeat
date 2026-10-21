"""Tests for the alarm state machine (src/heatbeat/alarms.py).

Example tests show single cases. Property tests (Hypothesis) generate hundreds
of random sequences and check rules that must hold for ALL of them.
"""

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from heatbeat.alarms import AlarmState, AlarmStatus, ThresholdRule, step

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
RULE = ThresholdRule(limit=80.0, delay=timedelta(minutes=5), hysteresis=2.0)


def feed(values: list[float], every: timedelta = timedelta(minutes=1)) -> AlarmStatus:
    status = AlarmStatus()
    for i, value in enumerate(values):
        status = step(RULE, status, value, T0 + i * every)
    return status


# --- examples ---------------------------------------------------------------


def test_single_spike_does_not_fire():
    assert feed([70, 95, 70]).state is AlarmState.NORMAL


def test_condition_held_long_enough_fires():
    assert feed([85] * 7).state is AlarmState.FIRING


def test_condition_shorter_than_delay_is_only_pending():
    assert feed([85] * 3).state is AlarmState.PENDING


def test_value_inside_hysteresis_band_keeps_firing():
    """79 is below the limit (80) but not below limit - hysteresis (78): no flapping."""
    assert feed([85] * 7 + [79]).state is AlarmState.FIRING


def test_value_below_hysteresis_band_clears():
    assert feed([85] * 7 + [77]).state is AlarmState.NORMAL


def test_s09_late_sample_does_not_change_the_decision():
    """S09: a reading that arrives late (older timestamp) is ignored."""
    firing = feed([85] * 7)

    after_late = step(RULE, firing, 10.0, T0)  # timestamp from the past

    assert after_late == firing


# --- properties -------------------------------------------------------------

values = st.lists(st.floats(min_value=-50, max_value=150), min_size=1, max_size=60)


@given(st.lists(st.floats(min_value=-50, max_value=80), min_size=1, max_size=60))
def test_property_never_leaves_normal_if_limit_is_never_exceeded(below):
    assert feed(below).state is AlarmState.NORMAL


@given(values)
def test_property_firing_needs_a_long_enough_streak(samples):
    """FIRING is only possible after `delay` minutes of consecutive values above the limit."""
    status = AlarmStatus()
    streak_start = None
    for i, value in enumerate(samples):
        ts = T0 + timedelta(minutes=i)
        before = status.state
        status = step(RULE, status, value, ts)
        if value > RULE.limit:
            streak_start = streak_start or ts
        elif status.state is not AlarmState.FIRING:
            streak_start = None
        if status.state is AlarmState.FIRING and before is not AlarmState.FIRING:
            assert streak_start is not None
            assert ts - streak_start >= RULE.delay


@given(values, st.floats(min_value=-50, max_value=150), st.integers(min_value=0, max_value=60))
def test_property_old_samples_never_change_state(samples, late_value, minutes_back):
    status = feed(samples)
    assert status.last_ts is not None

    late = step(RULE, status, late_value, status.last_ts - timedelta(minutes=minutes_back))

    assert late == status


# --- "below the limit" rules (low spread, low COP) --------------------------------

LOW = ThresholdRule(limit=3.0, delay=timedelta(minutes=5), hysteresis=0.5, above=False)


def feed_low(values: list[float]) -> AlarmStatus:
    status = AlarmStatus()
    for i, value in enumerate(values):
        status = step(LOW, status, value, T0 + i * timedelta(minutes=1))
    return status


def test_below_rule_fires_when_value_stays_low():
    assert feed_low([2.0] * 7).state is AlarmState.FIRING


def test_below_rule_clears_only_above_limit_plus_hysteresis():
    assert feed_low([2.0] * 7 + [3.2]).state is AlarmState.FIRING  # inside the band: no flapping
    assert feed_low([2.0] * 7 + [3.6]).state is AlarmState.NORMAL


@given(st.lists(st.floats(min_value=3.0, max_value=100), min_size=1, max_size=60))
def test_property_below_rule_never_fires_while_value_is_high_enough(values):
    assert feed_low(values).state is AlarmState.NORMAL
