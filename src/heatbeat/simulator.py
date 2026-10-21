"""Plant simulator: fake gas-boiler heating plants that publish over MQTT.

What this file does: models a gas boiler (heating curve, burner on/off with
hysteresis, flow and return temperature), can inject faults (short cycling,
heating curve too high, stuck sensor), and publishes readings exactly like a
real gateway would. A seed makes every run repeatable.

Run it with:  uv run heatbeat-simulator --plants 5 --backfill-hours 6
"""

import argparse
import json
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from heatbeat.config import Settings
from heatbeat.contract import telemetry_topic

log = logging.getLogger("heatbeat.simulator")

STEP = timedelta(seconds=30)


class Fault(StrEnum):
    SHORT_CYCLING = "short_cycling"  # oversized boiler, tiny hysteresis: many burner starts
    CURVE_TOO_HIGH = "curve_too_high"  # flow temperature far above what the building needs
    STUCK_SENSOR = "stuck_sensor"  # flow sensor always reports the same value


@dataclass(frozen=True)
class BoilerConfig:
    curve_level: float = 30.0  # flow set-point when it is 20 degC outside
    curve_slope: float = 1.4  # extra degrees of flow per degree colder outside
    hysteresis: float = 5.0  # burner switches at set-point +/- this
    heat_rate: float = 2.0  # degC per step while the burner is on
    fault: Fault | None = None


class GasBoilerPlant:
    """A simple, deterministic gas boiler model. One `step` = 30 seconds."""

    def __init__(self, seed: int, config: BoilerConfig | None = None) -> None:
        self.config = config or BoilerConfig()
        self._rng = random.Random(seed)
        self.flow = 40.0
        self.burner_on = False
        self.burner_starts = 0
        self._stuck_value: float | None = None

    def outdoor_temp(self, t: datetime) -> float:
        hours = t.hour + t.minute / 60
        daily = 6 * math.sin(2 * math.pi * (hours - 9) / 24)  # warmest mid-afternoon
        return 5 + daily + self._rng.gauss(0, 0.3)

    def setpoint(self, outdoor: float) -> float:
        cfg = self.config
        value = cfg.curve_level + cfg.curve_slope * (20 - outdoor)
        if cfg.fault is Fault.CURVE_TOO_HIGH:
            value += 15
        return max(20.0, min(80.0 + 15, value))

    def step(self, t: datetime) -> dict[str, float]:
        cfg = self.config
        outdoor = self.outdoor_temp(t)
        target = self.setpoint(outdoor)
        hysteresis = 1.0 if cfg.fault is Fault.SHORT_CYCLING else cfg.hysteresis
        heat_rate = cfg.heat_rate * 3 if cfg.fault is Fault.SHORT_CYCLING else cfg.heat_rate

        if self.burner_on and self.flow > target + hysteresis:
            self.burner_on = False
        elif not self.burner_on and self.flow < target - hysteresis:
            self.burner_on = True
            self.burner_starts += 1

        if self.burner_on:
            self.flow += heat_rate
        else:
            self.flow -= 0.03 * (self.flow - outdoor)  # cools towards outdoor temperature

        spread = 12.0 if self.burner_on else 5.0
        flow_reading = self.flow + self._rng.gauss(0, 0.2)
        if cfg.fault is Fault.STUCK_SENSOR:
            if self._stuck_value is None:
                self._stuck_value = round(flow_reading, 1)
            flow_reading = self._stuck_value

        return {
            "outdoor_temp": round(outdoor, 2),
            "flow_temp": round(flow_reading, 2),
            "return_temp": round(max(outdoor, self.flow - spread + self._rng.gauss(0, 0.2)), 2),
            "burner_state": 1.0 if self.burner_on else 0.0,
            "burner_modulation": round(self._rng.uniform(40, 100), 1) if self.burner_on else 0.0,
        }


class SimulatedGateway:
    """Wraps one plant and builds messages with gateway id, boot id and sequence number."""

    def __init__(self, index: int, plant: GasBoilerPlant) -> None:
        self.gateway = f"gw-{index:04d}"
        self.plant_key = f"p-{index:04d}"
        self.plant = plant
        self.boot_id = uuid.uuid4().hex[:12]  # new on every start, like a real reboot
        self.seq = 0

    @property
    def topic(self) -> str:
        return telemetry_topic(self.gateway, self.plant_key)

    def message(self, steps: list[tuple[datetime, dict[str, float]]], sent_at: datetime) -> dict[str, Any]:
        self.seq += 1
        return {
            "schema": 1,
            "gateway": self.gateway,
            "boot_id": self.boot_id,
            "seq": self.seq,
            "sent_at": sent_at.isoformat(),
            "plant": self.plant_key,
            "readings": [
                {"dp": dp, "t": t.isoformat(), "v": value, "q": 0}
                for t, values in steps
                for dp, value in values.items()
            ],
        }


def build_gateways(count: int, seed: int, with_faults: bool) -> list[SimulatedGateway]:
    faults = [None, Fault.SHORT_CYCLING, Fault.CURVE_TOO_HIGH, Fault.STUCK_SENSOR]
    gateways = []
    for i in range(1, count + 1):
        fault = faults[i % len(faults)] if with_faults else None
        plant = GasBoilerPlant(seed=seed + i, config=BoilerConfig(fault=fault))
        gateways.append(SimulatedGateway(i, plant))
    return gateways


def publish(client: mqtt.Client, gw: SimulatedGateway, payload: dict[str, Any]) -> mqtt.MQTTMessageInfo:
    return client.publish(gw.topic, json.dumps(payload), qos=1)


def backfill(client: mqtt.Client, gateways: list[SimulatedGateway], hours: float) -> None:
    """Upload past hours at once, like a gateway that was offline (a backlog)."""
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(hours=hours)
    info = None
    for gw in gateways:
        t, batch = start, []
        while t < now:
            batch.append((t, gw.plant.step(t)))
            if len(batch) == 20:
                info = publish(client, gw, gw.message(batch, now))
                batch = []
            t += STEP
        if batch:
            info = publish(client, gw, gw.message(batch, now))
    if info is not None:
        info.wait_for_publish(timeout=60)
    log.info("backfill of %.1f h for %d plants published", hours, len(gateways))


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate heating plants over MQTT")
    parser.add_argument("--plants", type=int, default=5)
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between live readings")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--backfill-hours", type=float, default=0.0)
    parser.add_argument("--with-faults", action="store_true", help="give some plants a fault")
    parser.add_argument("--once", action="store_true", help="exit after the backfill")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="heatbeat-simulator", protocol=mqtt.MQTTv5)
    client.connect(settings.mqtt_host, settings.mqtt_port)
    client.loop_start()

    gateways = build_gateways(args.plants, args.seed, args.with_faults)
    if args.backfill_hours > 0:
        backfill(client, gateways, args.backfill_hours)

    try:
        while not args.once:
            now = datetime.now(UTC).replace(microsecond=0)
            for gw in gateways:
                publish(client, gw, gw.message([(now, gw.plant.step(now))], now))
            log.info("published live readings for %d plants", len(gateways))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
