"""Simplified physical models of heating plants.

What this file does: three plant types that step forward 30 seconds at a time.
Each plant heats a simple one-room building model
(C * dT_room/dt = Q_heat - UA * (T_room - T_out)) and follows a heating curve:
flow set-point = level + slope * (20 - T_out). Parameters (heating curve,
heating on/off) can be changed remotely by commands. Faults can be injected.
Each plant returns its data in its OWN vendor format (see adapters.py).
The models are deliberately simple: good enough for realistic shapes, not
for engineering decisions.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

STEP = timedelta(seconds=30)
_DT_H = STEP.total_seconds() / 3600  # one step in hours


class Fault(StrEnum):
    SHORT_CYCLING = "short_cycling"  # oversized boiler, tiny hysteresis: many burner starts
    CURVE_TOO_HIGH = "curve_too_high"  # flow temperature far above what the building needs
    STUCK_SENSOR = "stuck_sensor"  # flow sensor always reports the same value
    LOW_SPREAD = "low_spread"  # hydraulic problem: return almost as hot as flow
    COP_DEGRADATION = "cop_degradation"  # heat pump efficiency far below normal


class PlantType(StrEnum):
    GAS_BOILER = "gas_boiler"
    HEAT_PUMP = "heat_pump"
    DISTRICT_HEATING = "district_heating"


def plant_type_for(index: int) -> PlantType:
    """Which plant type the simulated plant number `index` is (1-based, repeating)."""
    return list(PlantType)[(index - 1) % 3]


@dataclass
class Building:
    """One-capacity building model: a heat store that loses heat to the outside."""

    room: float = 20.5  # degC
    capacity_kwh_per_k: float = 12.0  # C
    loss_kw_per_k: float = 0.45  # UA

    def step(self, heat_kw: float, outdoor: float) -> float:
        loss = self.loss_kw_per_k * (self.room - outdoor)
        self.room += (heat_kw - loss) * _DT_H / self.capacity_kwh_per_k
        return self.room


@dataclass
class Plant:
    """Shared parts: weather, heating curve, adjustable parameters, building."""

    seed: int
    fault: Fault | None = None
    params: dict[str, float] = field(default_factory=dict)
    plant_type: PlantType = PlantType.GAS_BOILER

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self.building = Building()
        defaults = {"heating_curve_level": 26.0, "heating_curve_slope": 1.0, "heating_enabled": 1.0}
        self.params = {**defaults, **self.params}

    def outdoor_temp(self, t: datetime) -> float:
        hours = t.hour + t.minute / 60
        daily = 6 * math.sin(2 * math.pi * (hours - 9) / 24)  # warmest mid-afternoon
        return 5 + daily + self._rng.gauss(0, 0.3)

    def setpoint(self, outdoor: float) -> float:
        level = self.params["heating_curve_level"] + (15 if self.fault is Fault.CURVE_TOO_HIGH else 0)
        value = level + self.params["heating_curve_slope"] * (20 - outdoor)
        return max(20.0, min(90.0, value))

    @property
    def heating_on(self) -> bool:
        return self.params["heating_enabled"] >= 0.5

    def radiator_heat(self, flow: float, ret: float) -> float:
        """Heat the radiators give to the rooms (kW), grows with (mean water temp - room)."""
        mean = (flow + ret) / 2
        return max(0.0, 0.32 * (mean - self.building.room) ** 1.1) if mean > self.building.room else 0.0

    def noise(self, sigma: float = 0.2) -> float:
        return self._rng.gauss(0, sigma)

    def native(self, t: datetime) -> dict[Any, Any]:
        raise NotImplementedError


class GasBoiler(Plant):
    """Gas boiler: burner on/off with hysteresis around the heating-curve set-point."""

    NOMINAL_KW = 24.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.plant_type = PlantType.GAS_BOILER
        self.flow = 40.0
        self.burner_on = False
        self.burner_starts = 0
        self._stuck: float | None = None

    def native(self, t: datetime) -> dict[str, float]:
        """One step. Returns 'Vendor A' JSON with German short keys."""
        outdoor = self.outdoor_temp(t)
        target = self.setpoint(outdoor)
        short = self.fault is Fault.SHORT_CYCLING
        hysteresis = 1.0 if short else 5.0
        heat_rate = 6.0 if short else 2.0

        if not self.heating_on:
            self.burner_on = False
        elif self.burner_on and self.flow > target + hysteresis:
            self.burner_on = False
        elif not self.burner_on and self.flow < target - hysteresis:
            self.burner_on = True
            self.burner_starts += 1

        if self.burner_on:
            self.flow += heat_rate
        else:
            self.flow -= 0.03 * (self.flow - outdoor)

        spread = 1.5 if self.fault is Fault.LOW_SPREAD else (12.0 if self.burner_on else 5.0)
        ret = max(outdoor, self.flow - spread)
        room = self.building.step(self.radiator_heat(self.flow, ret) if self.heating_on else 0.0, outdoor)
        modulation = self._rng.uniform(40, 100) if self.burner_on else 0.0

        flow_reading = self.flow + self.noise()
        if self.fault is Fault.STUCK_SENSOR:
            self._stuck = self._stuck if self._stuck is not None else round(flow_reading, 1)
            flow_reading = self._stuck

        return {
            "AT": round(outdoor, 2),  # Aussentemperatur
            "VL": round(flow_reading, 2),  # Vorlauf (flow)
            "RL": round(ret + self.noise(), 2),  # Ruecklauf (return)
            "RT": round(room, 2),  # Raumtemperatur
            "BR": 1 if self.burner_on else 0,  # Brenner
            "MOD": round(modulation, 1),
            "LEIST": round(self.NOMINAL_KW * modulation / 100, 2),  # Leistung kW
            "HK_NIV": self.params["heating_curve_level"],
            "HK_STEIL": self.params["heating_curve_slope"],
            "HZG": int(self.heating_on),
        }


class HeatPump(Plant):
    """Air-source heat pump: COP from a Carnot fraction, defrost cycles in the cold."""

    HEAT_KW = 9.0
    ETA = 0.45  # fraction of the ideal (Carnot) efficiency

    def __post_init__(self) -> None:
        if "heating_curve_level" not in self.params:
            self.params = {"heating_curve_level": 30.0, "heating_curve_slope": 0.8, **self.params}
        super().__post_init__()
        self.plant_type = PlantType.HEAT_PUMP
        self.flow = 30.0
        self.compressor_on = False
        self.run_steps = 0  # compressor steps since the last defrost
        self.defrost_left = 0
        self.defrosts = 0

    def cop(self, flow: float, source: float) -> float:
        eta = 0.28 if self.fault is Fault.COP_DEGRADATION else self.ETA
        t_flow_k = flow + 273.15
        lift = max(5.0, flow - source)  # avoid dividing by ~0
        return max(1.0, min(7.0, eta * t_flow_k / lift))

    def native(self, t: datetime) -> dict[int, int]:
        """One step. Returns 'Vendor B' Modbus-style registers (scaled integers)."""
        outdoor = self.outdoor_temp(t)
        target = self.setpoint(outdoor)

        if self.defrost_left > 0:
            self.defrost_left -= 1
        elif not self.heating_on:
            self.compressor_on = False
        elif self.compressor_on and self.flow > target + 3:
            self.compressor_on = False
        elif not self.compressor_on and self.flow < target - 3:
            self.compressor_on = True

        defrosting = self.defrost_left > 0
        if self.compressor_on and not defrosting:
            self.run_steps += 1
            if outdoor < 3 and self.run_steps >= 120:  # about 1 h of running in the cold
                self.defrost_left, self.run_steps = 10, 0
                self.defrosts += 1

        if defrosting:
            heat, self.flow = 0.0, self.flow - 0.8
        elif self.compressor_on:
            heat, self.flow = self.HEAT_KW, self.flow + 1.0
        else:
            heat, self.flow = 0.0, self.flow - 0.03 * (self.flow - outdoor)

        cop = self.cop(self.flow, outdoor) if heat > 0 else 0.0
        electrical = heat / cop if cop else (1.5 if defrosting else 0.0)
        ret = max(outdoor, self.flow - (5.0 if self.compressor_on else 2.0))
        room = self.building.step(self.radiator_heat(self.flow, ret) if self.heating_on else 0.0, outdoor)

        def reg(value: float, scale: int = 10) -> int:
            return round(value * scale) & 0xFFFF  # 16-bit register, two's complement

        return {
            30001: reg(outdoor),
            30002: reg(self.flow + self.noise()),
            30003: reg(ret + self.noise()),
            30004: reg(room),
            30010: int(self.compressor_on),
            30011: int(defrosting),
            30020: reg(heat, 100),  # kW x 100
            30021: reg(electrical, 100),
            30030: reg(self.params["heating_curve_level"]),
            30031: reg(self.params["heating_curve_slope"], 100),
            30032: int(self.heating_on),
        }


class DistrictHeating(Plant):
    """District-heating substation: a valve mixes hot network water into the building loop."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.plant_type = PlantType.DISTRICT_HEATING
        self.valve = 30.0  # percent open
        self.flow = 45.0

    def native(self, t: datetime) -> dict[str, Any]:
        """One step. Returns 'Vendor C' nested JSON."""
        outdoor = self.outdoor_temp(t)
        primary_supply = 75 + 0.8 * max(0.0, 10 - outdoor)  # network runs hotter when it is cold
        target = self.setpoint(outdoor) if self.heating_on else 0.0

        # Simple proportional valve controller.
        self.valve = max(0.0, min(100.0, self.valve + 1.5 * (target - self.flow)))
        reachable = outdoor + (primary_supply - outdoor) * (self.valve / 100) ** 0.5
        self.flow += 0.3 * (reachable - self.flow)

        spread = 3.0 if self.fault is Fault.LOW_SPREAD else 15.0
        secondary_return = max(outdoor, self.flow - spread * self.valve / 100)
        primary_return = secondary_return + 4
        heat = self.radiator_heat(self.flow, secondary_return) if self.heating_on else 0.0
        room = self.building.step(heat, outdoor)

        return {
            "ambient": {"temperature": round(outdoor, 2)},
            "primary": {"supply": round(primary_supply, 2), "return": round(primary_return, 2)},
            "secondary": {"supply": round(self.flow + self.noise(), 2), "return": round(secondary_return, 2)},
            "room": round(room, 2),
            "valve_pct": round(self.valve, 1),
            "settings": {
                "curve_level": self.params["heating_curve_level"],
                "curve_slope": self.params["heating_curve_slope"],
                "heating": bool(self.heating_on),
            },
        }


def make_plant(plant_type: PlantType, seed: int, fault: Fault | None = None) -> Plant:
    cls: type[Plant] = {
        PlantType.GAS_BOILER: GasBoiler,
        PlantType.HEAT_PUMP: HeatPump,
        PlantType.DISTRICT_HEATING: DistrictHeating,
    }[plant_type]
    return cls(seed=seed, fault=fault)
