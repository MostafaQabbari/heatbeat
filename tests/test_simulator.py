"""Tests for the plant simulator (src/heatbeat/simulator.py)."""

from datetime import UTC, datetime, timedelta

from heatbeat.contract import TelemetryMessage
from heatbeat.simulator import STEP, BoilerConfig, Fault, GasBoilerPlant, SimulatedGateway

START = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def run(plant: GasBoilerPlant, hours: float) -> list[dict[str, float]]:
    steps = int(timedelta(hours=hours) / STEP)
    return [plant.step(START + i * STEP) for i in range(steps)]


def test_same_seed_gives_identical_data():
    """Reproducibility: every scenario can be replayed exactly."""
    assert run(GasBoilerPlant(seed=1), 2) == run(GasBoilerPlant(seed=1), 2)


def test_different_seed_gives_different_data():
    assert run(GasBoilerPlant(seed=1), 2) != run(GasBoilerPlant(seed=2), 2)


def test_values_stay_in_plausible_ranges():
    for values in run(GasBoilerPlant(seed=3), 24):
        assert -40 <= values["outdoor_temp"] <= 50
        assert 0 <= values["flow_temp"] <= 110
        assert values["return_temp"] <= values["flow_temp"] + 1
        assert values["burner_state"] in (0.0, 1.0)


def test_short_cycling_fault_causes_many_more_burner_starts():
    normal = GasBoilerPlant(seed=4)
    faulty = GasBoilerPlant(seed=4, config=BoilerConfig(fault=Fault.SHORT_CYCLING))

    run(normal, 6)
    run(faulty, 6)

    assert faulty.burner_starts > 2 * normal.burner_starts


def test_curve_too_high_fault_raises_flow_temperature():
    normal = run(GasBoilerPlant(seed=5), 6)
    faulty = run(GasBoilerPlant(seed=5, config=BoilerConfig(fault=Fault.CURVE_TOO_HIGH)), 6)

    def mean_flow(rows):
        return sum(r["flow_temp"] for r in rows) / len(rows)

    assert mean_flow(faulty) > mean_flow(normal) + 10


def test_stuck_sensor_reports_one_value():
    rows = run(GasBoilerPlant(seed=6, config=BoilerConfig(fault=Fault.STUCK_SENSOR)), 2)

    assert len({r["flow_temp"] for r in rows}) == 1


def test_gateway_messages_follow_the_contract_and_count_up():
    gw = SimulatedGateway(7, GasBoilerPlant(seed=7))
    step = (START, gw.plant.step(START))

    first = TelemetryMessage.model_validate(gw.message([step], START))
    second = TelemetryMessage.model_validate(gw.message([step], START))

    assert first.gateway == "gw-0007"
    assert first.plant == "p-0007"
    assert (first.seq, second.seq) == (1, 2)
    assert first.boot_id == second.boot_id
