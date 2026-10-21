"""Tests for the plant models and the simulator (src/heatbeat/sim/, src/heatbeat/simulator.py)."""

from datetime import UTC, datetime, timedelta

from heatbeat.contract import TelemetryMessage
from heatbeat.sim.adapters import ADAPTERS
from heatbeat.sim.gateway import SimulatedGateway
from heatbeat.sim.plants import STEP, Fault, GasBoiler, HeatPump, PlantType, make_plant, plant_type_for
from heatbeat.simulator import build_gateways, fault_for

START = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def run(plant_type: PlantType, hours: float, seed: int = 1, fault: Fault | None = None):
    plant = make_plant(plant_type, seed, fault)
    adapter = ADAPTERS[plant_type]
    rows = [adapter.normalize(plant.native(START + i * STEP)) for i in range(int(timedelta(hours=hours) / STEP))]
    return plant, rows


def mean(rows, key):
    values = [r[key] for r in rows if key in r]
    return sum(values) / len(values)


def test_same_seed_gives_identical_data():
    """Reproducibility: every scenario can be replayed exactly."""
    assert run(PlantType.HEAT_PUMP, 3, seed=1)[1] == run(PlantType.HEAT_PUMP, 3, seed=1)[1]


def test_different_seed_gives_different_data():
    assert run(PlantType.GAS_BOILER, 3, seed=1)[1] != run(PlantType.GAS_BOILER, 3, seed=2)[1]


def test_plant_types_take_turns():
    assert [plant_type_for(i) for i in (1, 2, 3, 4)] == [
        PlantType.GAS_BOILER,
        PlantType.HEAT_PUMP,
        PlantType.DISTRICT_HEATING,
        PlantType.GAS_BOILER,
    ]


def test_healthy_plants_keep_rooms_comfortable():
    for plant_type in PlantType:
        _, rows = run(plant_type, 24)
        assert 19.5 < mean(rows[240:], "room_temp") < 23.0, plant_type


def test_short_cycling_fault_causes_many_more_burner_starts():
    normal, _ = run(PlantType.GAS_BOILER, 12)
    faulty, _ = run(PlantType.GAS_BOILER, 12, fault=Fault.SHORT_CYCLING)

    assert isinstance(normal, GasBoiler) and isinstance(faulty, GasBoiler)
    assert faulty.burner_starts > 2 * normal.burner_starts


def test_curve_too_high_overheats_the_rooms():
    _, normal = run(PlantType.DISTRICT_HEATING, 24)
    _, faulty = run(PlantType.DISTRICT_HEATING, 24, fault=Fault.CURVE_TOO_HIGH)

    assert mean(faulty[240:], "room_temp") > mean(normal[240:], "room_temp") + 3


def test_stuck_sensor_reports_one_value():
    _, rows = run(PlantType.GAS_BOILER, 2, fault=Fault.STUCK_SENSOR)

    assert len({r["flow_temp"] for r in rows}) == 1


def test_low_spread_fault_shrinks_flow_minus_return():
    _, rows = run(PlantType.GAS_BOILER, 6, fault=Fault.LOW_SPREAD)

    assert mean(rows, "flow_temp") - mean(rows, "return_temp") < 2.5


def test_cop_degradation_lowers_heat_pump_efficiency():
    _, normal = run(PlantType.HEAT_PUMP, 24)
    _, faulty = run(PlantType.HEAT_PUMP, 24, fault=Fault.COP_DEGRADATION)

    assert mean(faulty, "cop") < mean(normal, "cop") - 1


def test_heat_pump_defrosts_in_the_cold():
    plant, _ = run(PlantType.HEAT_PUMP, 24)

    assert isinstance(plant, HeatPump)
    assert plant.defrosts > 0


def test_summer_mode_switches_heating_off():
    plant = make_plant(PlantType.GAS_BOILER, seed=1)
    plant.params["heating_enabled"] = 0.0
    adapter = ADAPTERS[PlantType.GAS_BOILER]

    rows = [adapter.normalize(plant.native(START + i * STEP)) for i in range(240)]

    assert all(r["burner_state"] == 0 for r in rows)


def test_first_three_plants_are_healthy_then_faults_take_turns():
    assert [fault_for(i, with_faults=True) for i in (1, 2, 3)] == [None, None, None]
    assert fault_for(4, with_faults=True) is Fault.SHORT_CYCLING
    assert fault_for(4, with_faults=False) is None


def test_gateway_messages_follow_the_contract_and_count_up():
    gw = SimulatedGateway(7, make_plant(PlantType.GAS_BOILER, seed=7))
    step = (START, gw.read(START))

    first = TelemetryMessage.model_validate(gw.message([step], START))
    second = TelemetryMessage.model_validate(gw.message([step], START))

    assert (first.gateway, first.plant) == ("gw-0007", "p-0007")
    assert (first.seq, second.seq) == (1, 2)
    assert first.boot_id == second.boot_id


def test_build_gateways_gives_unique_ids():
    gateways = build_gateways(9, seed=1, with_faults=True)

    assert len({g.gateway for g in gateways}) == 9
