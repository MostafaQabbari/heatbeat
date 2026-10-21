"""Tests for the alarm rule types (src/heatbeat/alarm_rules.py).

The most important test is the fault matrix at the bottom: every healthy
simulated plant raises NO alarm, and every injected fault raises ITS alarm.
"""

from datetime import UTC, datetime, timedelta

import pytest

from heatbeat.alarm_rules import DEFAULT_RULES, RuleKind, RuleSpec, derive_signal, evaluate
from heatbeat.alarms import AlarmStatus
from heatbeat.sim.adapters import ADAPTERS
from heatbeat.sim.plants import STEP, Fault, PlantType, make_plant

T0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
M = timedelta(minutes=1)


def series(values, every=M):
    return [(T0 + i * every, float(v)) for i, v in enumerate(values)]


def rule(kind, **kw):
    defaults = {"name": "r", "kind": kind, "datapoint": "x", "limit": 1.0, "above": True, "delay": 0 * M}
    return RuleSpec(**{**defaults, **kw})


def test_spread_pairs_samples_by_timestamp():
    flow, ret = series([60, 61, 62]), series([40, 59, 50])

    signal = derive_signal(rule(RuleKind.SPREAD, second_datapoint="y"), flow, ret)

    assert [v for _, v in signal] == [20, 2, 12]


def test_rate_counts_switch_ons_per_hour():
    on_off = series([0, 1] * 24, every=5 * M)  # burner starts every 10 minutes, for 4 hours

    signal = derive_signal(rule(RuleKind.RATE, window=timedelta(hours=1)), on_off)

    assert signal  # only produced once a full hour of history exists
    assert all(5 <= v <= 7 for _, v in signal)  # about 6 starts per hour


def test_flatline_sees_a_stuck_sensor():
    stuck = series([42.1] * 40)
    alive = series([42.1, 42.3] * 20)

    window = rule(RuleKind.FLATLINE, window=30 * M)
    assert max(v for _, v in derive_signal(window, stuck)) == 0
    assert min(v for _, v in derive_signal(window, alive)) > 0


def test_rolling_mean_smooths_a_noisy_value():
    noisy = series([2, 4] * 60)

    signal = derive_signal(rule(RuleKind.THRESHOLD, window=timedelta(minutes=10)), noisy)

    assert all(2.8 <= v <= 3.2 for _, v in signal)


def test_absence_is_minutes_since_last_data():
    now = T0 + timedelta(minutes=25)

    signal = derive_signal(rule(RuleKind.ABSENCE, datapoint=None), [], now=now, last_seen=T0)

    assert signal == [(now, 25.0)]


def test_absence_without_any_data_yet_gives_no_signal():
    assert derive_signal(rule(RuleKind.ABSENCE, datapoint=None), [], now=T0, last_seen=None) == []


def test_evaluate_reports_fired_and_cleared():
    spec = rule(RuleKind.THRESHOLD, limit=80, delay=2 * M, hysteresis=2)

    status, transitions = evaluate(spec, AlarmStatus(), series([70, 85, 85, 85, 85, 77]))

    assert [t.kind for t in transitions] == ["fired", "cleared"]
    assert status.state == "normal"


# --- fault matrix -------------------------------------------------------------

EXPECTED = {
    (PlantType.GAS_BOILER, None): set(),
    (PlantType.GAS_BOILER, Fault.SHORT_CYCLING): {"Short cycling (burner starts per hour)"},
    (PlantType.GAS_BOILER, Fault.CURVE_TOO_HIGH): {"Rooms overheated (heating curve too high?)"},
    (PlantType.GAS_BOILER, Fault.LOW_SPREAD): {"Low spread flow/return (hydraulics)"},
    (PlantType.GAS_BOILER, Fault.STUCK_SENSOR): {"Flow sensor stuck"},
    (PlantType.HEAT_PUMP, None): set(),
    (PlantType.HEAT_PUMP, Fault.COP_DEGRADATION): {"Heat pump efficiency low (COP, 1 h average)"},
    (PlantType.DISTRICT_HEATING, None): set(),
    (PlantType.DISTRICT_HEATING, Fault.LOW_SPREAD): {"Low spread flow/return (hydraulics)"},
    (PlantType.DISTRICT_HEATING, Fault.CURVE_TOO_HIGH): {"Rooms overheated (heating curve too high?)"},
}


def fired_rules(plant_type: PlantType, fault: Fault | None, hours: int = 36) -> set[str]:
    plant, adapter = make_plant(plant_type, seed=11, fault=fault), ADAPTERS[plant_type]
    data: dict[str, list] = {}
    for i in range(hours * 120):
        t = T0 + i * STEP
        for key, value in adapter.normalize(plant.native(t)).items():
            data.setdefault(key, []).append((t, value))
    warm_up = T0 + timedelta(hours=2)  # the building model starts from a guess
    fired = set()
    for spec in DEFAULT_RULES:
        if spec.kind is RuleKind.ABSENCE or (spec.plant_type and spec.plant_type != plant_type.value):
            continue
        a = [s for s in data.get(spec.datapoint or "", []) if s[0] >= warm_up]
        b = data.get(spec.second_datapoint) if spec.second_datapoint else None
        _, transitions = evaluate(spec, AlarmStatus(), derive_signal(spec, a, b))
        if any(t.kind == "fired" for t in transitions):
            fired.add(spec.name)
    return fired


@pytest.mark.parametrize(("plant_type", "fault"), list(EXPECTED))
def test_fault_matrix_each_fault_raises_its_alarm_and_healthy_plants_none(plant_type, fault):
    assert fired_rules(plant_type, fault) == EXPECTED[(plant_type, fault)]
