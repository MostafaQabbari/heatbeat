"""A simulated gateway: the box in the boiler room.

What this file does: reads its plant through the vendor adapter, numbers each
message (boot_id + seq), and executes remote commands defensively:
- checks the value range AGAIN (defence in depth, the cloud may have a bug),
- never applies an expired command,
- applies each command_id at most once; a duplicate gets the same answer again.
"""

import uuid
from datetime import datetime
from typing import Any

from heatbeat.contract import CommandMessage, CommandResult
from heatbeat.sim.adapters import ADAPTERS
from heatbeat.sim.plants import Plant

# The gateway's own copy of what may be written, and the allowed range.
WRITABLE_LIMITS: dict[str, tuple[float, float]] = {
    "heating_curve_level": (15.0, 50.0),
    "heating_curve_slope": (0.2, 3.0),
    "heating_enabled": (0.0, 1.0),
}


class SimulatedGateway:
    def __init__(self, index: int, plant: Plant) -> None:
        self.gateway = f"gw-{index:04d}"
        self.plant_key = f"p-{index:04d}"
        self.plant = plant
        self.adapter = ADAPTERS[plant.plant_type]
        self.boot_id = uuid.uuid4().hex[:12]  # new on every start, like a real reboot
        self.seq = 0
        self._results: dict[str, CommandResult] = {}

    def read(self, t: datetime) -> dict[str, float]:
        """Advance the plant one step and return standard datapoints."""
        return self.adapter.normalize(self.plant.native(t))

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

    def handle_command(self, cmd: CommandMessage, now: datetime) -> CommandResult:
        if cmd.command_id in self._results:
            return self._results[cmd.command_id]  # duplicate delivery: same answer, not applied twice

        def result(status: str, reason: str | None = None, value: float | None = None) -> CommandResult:
            return CommandResult.model_validate(
                {
                    "command_id": cmd.command_id,
                    "gateway": self.gateway,
                    "status": status,
                    "applied_value": value,
                    "reason": reason,
                    "at": now,
                }
            )

        if now >= cmd.expires_at:
            outcome = result("expired", "command arrived after its expiry")
        elif cmd.plant != self.plant_key:
            outcome = result("rejected", f"unknown plant {cmd.plant}")
        elif cmd.dp not in WRITABLE_LIMITS:
            outcome = result("rejected", f"{cmd.dp} is not writable")
        elif not WRITABLE_LIMITS[cmd.dp][0] <= cmd.value <= WRITABLE_LIMITS[cmd.dp][1]:
            outcome = result("rejected", f"{cmd.value} outside {WRITABLE_LIMITS[cmd.dp]}")
        else:
            self.plant.params[cmd.dp] = cmd.value
            outcome = result("applied", value=cmd.value)

        self._results[cmd.command_id] = outcome
        return outcome
