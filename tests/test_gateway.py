"""Tests for the simulated gateway's command handling (src/heatbeat/sim/gateway.py), scenario S15.

The gateway is the last line of defence before a real boiler, so it checks again.
"""

from datetime import UTC, datetime, timedelta

from heatbeat.contract import CommandMessage
from heatbeat.sim.gateway import SimulatedGateway
from heatbeat.sim.plants import PlantType, make_plant

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def gateway() -> SimulatedGateway:
    return SimulatedGateway(1, make_plant(PlantType.GAS_BOILER, seed=1))


def command(value: float = 24.0, dp: str = "heating_curve_level", expires_in: float = 60, cid: str = "c-1"):
    return CommandMessage(
        command_id=cid, plant="p-0001", dp=dp, value=value, expires_at=NOW + timedelta(seconds=expires_in)
    )


def test_valid_command_is_applied():
    gw = gateway()

    result = gw.handle_command(command(), NOW)

    assert result.status == "applied"
    assert gw.plant.params["heating_curve_level"] == 24.0


def test_s15_duplicate_delivery_is_applied_only_once():
    gw = gateway()
    first = gw.handle_command(command(value=24.0), NOW)
    gw.plant.params["heating_curve_level"] = 99.0  # someone changed it locally in between

    second = gw.handle_command(command(value=24.0), NOW + timedelta(seconds=5))

    assert second == first  # same answer again ...
    assert gw.plant.params["heating_curve_level"] == 99.0  # ... and NOT applied a second time


def test_s15_expired_command_is_never_applied():
    """A gateway that was offline must not apply a stale change an hour later."""
    gw = gateway()

    result = gw.handle_command(command(expires_in=60), NOW + timedelta(hours=1))

    assert result.status == "expired"
    assert gw.plant.params["heating_curve_level"] == 26.0  # unchanged default


def test_gateway_rejects_out_of_range_even_if_the_cloud_sent_it():
    """Defence in depth: a bug in the cloud must not reach the boiler."""
    result = gateway().handle_command(command(value=90.0), NOW)

    assert result.status == "rejected"
    assert "outside" in str(result.reason)


def test_gateway_rejects_read_only_datapoint():
    assert gateway().handle_command(command(dp="flow_temp"), NOW).status == "rejected"


def test_gateway_rejects_command_for_another_plant():
    cmd = CommandMessage(command_id="c-9", plant="p-9999", dp="heating_curve_level", value=24, expires_at=NOW)

    assert gateway().handle_command(cmd, NOW - timedelta(seconds=1)).status == "rejected"
