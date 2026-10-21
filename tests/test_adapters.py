"""Tests for the vendor adapters (src/heatbeat/sim/adapters.py): three formats, one standard."""

from datetime import UTC, datetime

import pytest

from heatbeat.sim.adapters import ADAPTERS, VendorAJson, VendorBModbus, VendorCNested, int16
from heatbeat.sim.plants import PlantType, make_plant

T = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(("raw", "value"), [(0, 0), (215, 215), (0xFFEC, -20), (0x8000, -32768), (0x7FFF, 32767)])
def test_int16_two_complement(raw, value):
    """-2.0 degC arrives as register value 0xFFEC (65516) with scale 10."""
    assert int16(raw) == value


def test_vendor_a_maps_german_short_keys():
    values = VendorAJson().normalize({"VL": 62.4, "RL": 41.8, "AT": -3.5, "UNBEKANNT": 1})

    assert values == {"flow_temp": 62.4, "return_temp": 41.8, "outdoor_temp": -3.5}  # unknown keys dropped


def test_vendor_b_scales_registers_and_derives_cop():
    values = VendorBModbus().normalize({30001: 0xFFEC, 30002: 352, 30020: 900, 30021: 300})

    assert values["outdoor_temp"] == -2.0
    assert values["flow_temp"] == 35.2
    assert values["heat_power_kw"] == 9.0
    assert values["cop"] == 3.0  # 9 kW heat / 3 kW electricity


def test_vendor_c_flattens_nested_json():
    native = {
        "ambient": {"temperature": 4.0},
        "primary": {"supply": 85.0, "return": 40.0},
        "secondary": {"supply": 50.0, "return": 36.0},
        "room": 21.0,
        "valve_pct": 33.0,
        "settings": {"curve_level": 26, "curve_slope": 1.0, "heating": True},
    }

    values = VendorCNested().normalize(native)

    assert values["primary_supply_temp"] == 85.0
    assert values["flow_temp"] == 50.0
    assert values["heating_enabled"] == 1.0


@pytest.mark.parametrize("plant_type", list(PlantType))
def test_every_plant_type_delivers_the_shared_core_datapoints(plant_type):
    """Whatever the vendor, the cloud always gets these keys with the same meaning."""
    values = ADAPTERS[plant_type].normalize(make_plant(plant_type, seed=1).native(T))

    for key in ("outdoor_temp", "flow_temp", "return_temp", "room_temp", "heating_curve_level", "heating_enabled"):
        assert key in values
