"""Simulator command line: many simulated plants and gateways talking MQTT.

What this file does: builds N plants (gas boiler, heat pump, district heating,
in turn), optionally with faults, publishes their readings like real gateways,
uploads a backlog on request (like gateways after an outage), and answers
remote commands. A seed makes every run repeatable.

Run it with:  uv run heatbeat-simulator --plants 9 --backfill-hours 6 --with-faults
"""

import argparse
import json
import logging
import signal
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from pydantic import ValidationError

from heatbeat.config import Settings
from heatbeat.contract import CommandMessage, command_result_topic, gateway_from_topic, telemetry_topic
from heatbeat.sim.gateway import SimulatedGateway
from heatbeat.sim.plants import STEP, Fault, PlantType, make_plant, plant_type_for
from heatbeat.sim.publisher import ReliablePublisher, persistent_session

log = logging.getLogger("heatbeat.simulator")

FAULTS_BY_TYPE: dict[PlantType, list[Fault]] = {
    PlantType.GAS_BOILER: [Fault.SHORT_CYCLING, Fault.CURVE_TOO_HIGH, Fault.STUCK_SENSOR, Fault.LOW_SPREAD],
    PlantType.HEAT_PUMP: [Fault.COP_DEGRADATION, Fault.CURVE_TOO_HIGH],
    PlantType.DISTRICT_HEATING: [Fault.LOW_SPREAD, Fault.CURVE_TOO_HIGH],
}


def fault_for(index: int, with_faults: bool) -> Fault | None:
    """Plants 1 to 3 are always healthy; later plants get faults in turn."""
    round_no = (index - 1) // 3
    if not with_faults or round_no == 0:
        return None
    faults = FAULTS_BY_TYPE[plant_type_for(index)]
    return faults[(round_no - 1) % len(faults)]


def build_gateways(count: int, seed: int, with_faults: bool) -> list[SimulatedGateway]:
    return [
        SimulatedGateway(i, make_plant(plant_type_for(i), seed + i, fault_for(i, with_faults)))
        for i in range(1, count + 1)
    ]


class Simulation:
    def __init__(self, client: mqtt.Client, gateways: list[SimulatedGateway]) -> None:
        self.client = client
        self.by_key = {gw.gateway: gw for gw in gateways}
        self.lock = threading.Lock()  # plants are stepped by one thread and commanded by another
        self.out = ReliablePublisher(client)  # store-and-forward, like real gateway firmware

    def publish_readings(self, gw: SimulatedGateway, payload: dict[str, Any]) -> None:
        self.out.publish(telemetry_topic(gw.gateway, gw.plant_key), json.dumps(payload))

    def backfill(self, hours: float) -> None:
        """Upload past hours at once, like a gateway that was offline (a backlog)."""
        now = datetime.now(UTC).replace(microsecond=0)
        start = now - timedelta(hours=hours)
        for gw in self.by_key.values():
            t, batch = start, []
            while t < now:
                with self.lock:
                    batch.append((t, gw.read(t)))
                if len(batch) == 20:
                    self.publish_readings(gw, gw.message(batch, now))
                    batch = []
                t += STEP
            if batch:
                self.publish_readings(gw, gw.message(batch, now))
        if not self.out.wait_until_confirmed(timeout=300):
            log.warning("%d backlog messages not yet confirmed by the broker", self.out.pending)
        log.info("backfill of %.1f h for %d plants published", hours, len(self.by_key))

    def on_command(self, _c: mqtt.Client, _u: object, msg: mqtt.MQTTMessage) -> None:
        gateway = gateway_from_topic(msg.topic, "commands")
        gw = self.by_key.get(gateway or "")
        if gw is None:
            return
        try:
            cmd = CommandMessage.model_validate_json(msg.payload)
        except ValidationError:
            log.warning("ignoring invalid command on %s", msg.topic)
            return
        with self.lock:
            result = gw.handle_command(cmd, datetime.now(UTC))
        log.info("%s: command %s -> %s", gw.gateway, cmd.command_id, result.status)
        self.client.publish(command_result_topic(gw.gateway), result.model_dump_json(by_alias=True), qos=1)

    def live(self, interval: float, stop: threading.Event) -> None:
        while not stop.is_set():
            now = datetime.now(UTC).replace(microsecond=0)
            for gw in self.by_key.values():
                with self.lock:
                    values = gw.read(now)
                self.publish_readings(gw, gw.message([(now, values)], now))
            log.info("published live readings for %d plants", len(self.by_key))
            stop.wait(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate heating plants over MQTT")
    parser.add_argument("--plants", type=int, default=9)
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between live readings")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--backfill-hours", type=float, default=0.0)
    parser.add_argument("--with-faults", action="store_true", help="give plants 4 and up a fault")
    parser.add_argument("--once", action="store_true", help="exit after the backfill")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="heatbeat-simulator", protocol=mqtt.MQTTv5)

    def on_connect(c: mqtt.Client, _u: object, _f: object, _r: object, _p: object) -> None:
        c.subscribe("hb/v1/+/commands", qos=1)

    client.on_connect = on_connect  # set BEFORE the publisher wraps it
    sim = Simulation(client, build_gateways(args.plants, args.seed, args.with_faults))
    client.on_message = sim.on_command
    client.connect(settings.mqtt_host, settings.mqtt_port, clean_start=False, properties=persistent_session())
    client.loop_start()

    # Ctrl+C only sets a flag; the loop then stops between two messages. (An exception
    # in the middle of a publish could leave the MQTT client's internal locks held.)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        if args.backfill_hours > 0:
            sim.backfill(args.backfill_hours)
        if not args.once:
            sim.live(args.interval, stop)
    finally:
        sim.out.wait_until_confirmed(timeout=5)
        client.disconnect()
        client.loop_stop()
        log.info("simulator stopped")


if __name__ == "__main__":
    main()
