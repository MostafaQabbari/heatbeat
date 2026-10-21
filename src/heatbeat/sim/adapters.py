"""Southbound adapters: three vendor formats in, one standard format out.

What this file does: every manufacturer speaks its own language. Vendor A
sends JSON with German short keys, vendor B exposes Modbus-style 16-bit
registers with scale factors, vendor C sends nested JSON. Each adapter maps
its vendor's data to the SAME datapoint keys from the catalogue
(flow_temp, return_temp, ...). Everything after the gateway only ever sees
the standard format: this is what "vendor-neutral" means.
"""

from typing import Any, ClassVar, Protocol

from heatbeat.sim.plants import PlantType


class Adapter(Protocol):
    def normalize(self, native: Any) -> dict[str, float]: ...


def int16(raw: int) -> int:
    """A 16-bit register is unsigned on the wire; values >= 0x8000 are negative."""
    return raw - 0x10000 if raw >= 0x8000 else raw


class VendorAJson:
    """Gas boilers: flat JSON with German abbreviations."""

    MAPPING: ClassVar[dict[str, str]] = {
        "AT": "outdoor_temp",
        "VL": "flow_temp",
        "RL": "return_temp",
        "RT": "room_temp",
        "BR": "burner_state",
        "MOD": "burner_modulation",
        "LEIST": "burner_power_kw",
        "HK_NIV": "heating_curve_level",
        "HK_STEIL": "heating_curve_slope",
        "HZG": "heating_enabled",
    }

    def normalize(self, native: dict[str, float]) -> dict[str, float]:
        return {self.MAPPING[k]: float(v) for k, v in native.items() if k in self.MAPPING}


class VendorBModbus:
    """Heat pumps: register number -> (datapoint, scale factor)."""

    REGISTERS: ClassVar[dict[int, tuple[str, int]]] = {
        30001: ("outdoor_temp", 10),
        30002: ("flow_temp", 10),
        30003: ("return_temp", 10),
        30004: ("room_temp", 10),
        30010: ("compressor_state", 1),
        30011: ("defrost_state", 1),
        30020: ("heat_power_kw", 100),
        30021: ("electrical_power_kw", 100),
        30030: ("heating_curve_level", 10),
        30031: ("heating_curve_slope", 100),
        30032: ("heating_enabled", 1),
    }

    def normalize(self, native: dict[int, int]) -> dict[str, float]:
        values = {}
        for register, raw in native.items():
            if register in self.REGISTERS:
                key, scale = self.REGISTERS[register]
                values[key] = round(int16(raw) / scale, 3)
        heat, electrical = values.get("heat_power_kw", 0.0), values.get("electrical_power_kw", 0.0)
        if heat > 0 and electrical > 0:
            values["cop"] = round(heat / electrical, 2)  # derived datapoint
        return values


class VendorCNested:
    """District-heating substations: nested JSON."""

    def normalize(self, native: dict[str, Any]) -> dict[str, float]:
        return {
            "outdoor_temp": float(native["ambient"]["temperature"]),
            "primary_supply_temp": float(native["primary"]["supply"]),
            "primary_return_temp": float(native["primary"]["return"]),
            "flow_temp": float(native["secondary"]["supply"]),
            "return_temp": float(native["secondary"]["return"]),
            "room_temp": float(native["room"]),
            "valve_position": float(native["valve_pct"]),
            "heating_curve_level": float(native["settings"]["curve_level"]),
            "heating_curve_slope": float(native["settings"]["curve_slope"]),
            "heating_enabled": 1.0 if native["settings"]["heating"] else 0.0,
        }


ADAPTERS: dict[PlantType, Adapter] = {
    PlantType.GAS_BOILER: VendorAJson(),
    PlantType.HEAT_PUMP: VendorBModbus(),
    PlantType.DISTRICT_HEATING: VendorCNested(),
}
