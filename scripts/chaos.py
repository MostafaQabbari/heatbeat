"""Chaos test (S22): break one service at a time under load and measure what happens.

What this file does: acts like a few gateways that send one message per second
each (and, like real gateways, keep unsent messages and retry them when the
broker is back). For each scenario it stops or kills one service, waits, starts
it again, and then checks:
  - lost readings: everything that was sent must end up in the database
  - duplicates: no reading may be stored twice
  - recovery time: from "service started again" until all readings are stored
The result is a Markdown table (docs/chaos-report.md).

Default commands use Docker Compose (run with: docker compose --profile app up -d):
    uv run python scripts/chaos.py
Other environments can pass their own stop/start commands with --commands file.json.
"""

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import psycopg
from paho.mqtt.enums import CallbackAPIVersion

from heatbeat.config import Settings
from heatbeat.contract import telemetry_topic
from heatbeat.sim.publisher import ReliablePublisher, persistent_session

log = logging.getLogger("chaos")

SCENARIOS = {
    "bridge-crash": {"stop": "docker compose kill bridge", "start": "docker compose start bridge"},
    "database-down": {"stop": "docker compose stop db", "start": "docker compose start db"},
    "broker-restart": {"stop": "docker compose stop mqtt", "start": "docker compose start mqtt"},
}


class Gateways:
    """N gateways with store-and-forward (ReliablePublisher): a reading is only "sent" when the broker confirmed it."""

    def __init__(self, client: mqtt.Client, count: int, run_id: str) -> None:
        self.out = ReliablePublisher(client)
        self.count, self.run_id = count, run_id
        self.produced = 0

    def tick(self, seq: int) -> None:
        now = datetime.now(UTC)  # full precision: every reading has its own timestamp
        for g in range(1, self.count + 1):
            gw, plant = f"gw-chaos-{g}", f"p-chaos-{self.run_id}-{g}"
            payload = {
                "schema": 1,
                "gateway": gw,
                "boot_id": self.run_id,
                "seq": seq,
                "sent_at": now.isoformat(),
                "plant": plant,
                "readings": [{"dp": "flow_temp", "t": now.isoformat(), "v": 40 + g}],
            }
            self.out.publish(telemetry_topic(gw, plant), json.dumps(payload))
            self.produced += 1


def stored(url: str, run_id: str) -> tuple[int, int] | None:
    """(rows, distinct keys) for this run, or None while the database is unreachable."""
    try:
        with psycopg.connect(url, connect_timeout=2) as conn:
            row = conn.execute(
                """
                SELECT count(*), count(DISTINCT (m.plant_id, m.datapoint_id, m.ts))
                FROM measurement m JOIN plant p ON p.id = m.plant_id WHERE p.key LIKE %s
                """,
                (f"p-chaos-{run_id}-%",),
            ).fetchone()
            return (int(row[0]), int(row[1])) if row else (0, 0)
    except psycopg.Error:
        return None


def provision(url: str, count: int) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO tenant (key, name) VALUES ('chaos', 'Chaos test') "
            "ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ).fetchone()[0]  # type: ignore[index]
        for g in range(1, count + 1):
            conn.execute(
                "INSERT INTO gateway (key, tenant_id) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (f"gw-chaos-{g}", tenant),
            )


def run_scenario(name: str, cmds: dict[str, str], args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    run_id = f"{name[:6]}{int(time.time()) % 100000}"
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"chaos-{run_id}", protocol=mqtt.MQTTv5)
    client.reconnect_delay_set(1, 2)
    gateways = Gateways(client, args.gateways, run_id)
    client.connect(settings.mqtt_host, settings.mqtt_port, clean_start=False, properties=persistent_session())
    client.loop_start()
    stop_load = threading.Event()

    def load() -> None:
        seq = 0
        while not stop_load.is_set():
            seq += 1
            gateways.tick(seq)
            stop_load.wait(1.0)

    loader = threading.Thread(target=load, daemon=True)
    loader.start()
    time.sleep(args.warmup)
    log.info("%s: stopping (%s)", name, cmds["stop"])
    subprocess.run(cmds["stop"], shell=True, check=False)  # noqa: S602 (commands come from the operator)
    time.sleep(args.outage)
    log.info("%s: starting (%s)", name, cmds["start"])
    subprocess.run(cmds["start"], shell=True, check=False)  # noqa: S602
    restarted = time.monotonic()
    time.sleep(args.after)
    stop_load.set()
    loader.join()

    deadline = time.monotonic() + args.timeout
    result = None
    while time.monotonic() < deadline:
        gateways.out.wait_until_confirmed(timeout=0.5)
        result = stored(settings.database_url, run_id)
        if result and result[0] >= gateways.produced:
            break
    caught_up = bool(result and result[0] >= gateways.produced)
    recovery = time.monotonic() - restarted - args.after if caught_up else None
    client.disconnect()
    client.loop_stop()

    rows, distinct = result or (0, 0)
    return {
        "scenario": name,
        "sent": gateways.produced,
        "stored": rows,
        "resent": gateways.out.resent_after_reconnect,
        "lost": max(0, gateways.produced - rows),
        "duplicates": rows - distinct,
        "caught_up_after_s": None if recovery is None else round(max(0.0, recovery), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateways", type=int, default=5)
    parser.add_argument("--warmup", type=float, default=10)
    parser.add_argument("--outage", type=float, default=20, help="seconds the service stays down")
    parser.add_argument("--after", type=float, default=10, help="seconds of load after the restart")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--commands", type=Path, help="JSON file with stop/start commands per scenario")
    parser.add_argument("--report", type=Path, default=Path("docs/chaos-report.md"))
    parser.add_argument("--environment", default="Docker Compose on the developer machine")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    scenarios = json.loads(args.commands.read_text()) if args.commands else SCENARIOS
    settings = Settings()
    provision(settings.database_url, args.gateways)
    results = [run_scenario(name, cmds, args, settings) for name, cmds in scenarios.items()]

    lines = [
        "# Chaos report (S22)",
        "",
        f"Measured {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Environment: {args.environment}.",
        f"Load: {args.gateways} gateways x 1 message/s with store-and-forward; each service down for "
        f"{args.outage:.0f} s while the load continues. Re-run with `uv run python scripts/chaos.py`.",
        "",
        "| Scenario | Readings produced | Stored | Lost | Duplicates | Re-sent by gateways | All stored after |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        caught = "not within timeout" if r["caught_up_after_s"] is None else f"{r['caught_up_after_s']} s"
        lines.append(
            f"| {r['scenario']} | {r['sent']} | {r['stored']} | {r['lost']} | {r['duplicates']} "
            f"| {r['resent']} | {caught} |"
        )
    lines += [
        "",
        "History: the first run lost 145 of 200 readings in broker-restart. Cause: the test broker",
        "could not write its session file, so after the restart it had forgotten the bridge's",
        "subscription. Details and the other two findings on the way (a deadlock, a wrong first",
        "explanation) in docs/adr/0010-gateway-store-and-forward.md.",
    ]
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    sys.exit(0 if all(r["lost"] == 0 and r["duplicates"] == 0 for r in results) else 1)


if __name__ == "__main__":
    main()
