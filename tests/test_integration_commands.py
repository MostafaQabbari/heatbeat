"""Integration tests: remote parameter changes end to end through API, database and command store.

Scenarios: S15 (lost or duplicate gateway answers, expiry), S16 (two people change
the same parameter), four-eyes approval, append-only audit trail.
"""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from helpers import count, make_message, make_user, provision, topic_for

from heatbeat.api.main import create_app
from heatbeat.auth import Role
from heatbeat.commands import CommandStore
from heatbeat.contract import CommandMessage, CommandResult, TelemetryMessage
from heatbeat.db import PostgresSink

pytestmark = pytest.mark.integration
PW = "pw-commands-123"


@pytest.fixture
def setup(db, test_settings):
    """Tenant with a viewer, an operator and two admins; plant p-17 reports heating_curve_level = 26."""
    tenant = provision(db, "gw-0042")
    now = datetime.now(UTC).replace(microsecond=0)
    msg = make_message(readings=[{"dp": "heating_curve_level", "t": now.isoformat(), "v": 26.0}], at=now)
    sink = PostgresSink(db)
    sink.write([(topic_for(msg), TelemetryMessage.model_validate(msg))], [])
    sink.close()
    users = (
        ("op@a.example", Role.OPERATOR),
        ("ad1@a.example", Role.ADMIN),
        ("ad2@a.example", Role.ADMIN),
        ("view@a.example", Role.VIEWER),
    )
    for email, role in users:
        make_user(db, tenant, email, role, password=PW)
    with TestClient(create_app(test_settings)) as client:
        yield client, db


def login(client: TestClient, email: str) -> dict[str, str]:
    token = client.post("/v1/auth/token", data={"username": email, "password": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def request(client, who, value, version=0, dp="heating_curve_level"):
    return client.post(
        "/v1/plants/p-17/commands", json={"datapoint": dp, "value": value, "expected_version": version}, headers=who
    )


def store(db: str) -> CommandStore:
    return CommandStore(psycopg.connect(db, autocommit=True))


def answer(command_id: str, status: str = "applied", value: float | None = 24.0, gateway: str = "gw-0042"):
    return CommandResult.model_validate(
        {
            "command_id": command_id,
            "gateway": gateway,
            "status": status,
            "applied_value": value,
            "at": datetime.now(UTC),
        }
    )


def test_small_change_goes_straight_to_the_gateway(setup):
    client, db = setup
    created = request(client, login(client, "op@a.example"), 24.0)
    published: list[tuple[str, CommandMessage]] = []

    sent = store(db).publish_due(lambda gw, m: published.append((gw, m)) or True, datetime.now(UTC))

    assert created.status_code == 201
    assert created.json()["state"] == "requested"
    assert sent == 1
    gateway, message = published[0]
    assert (gateway, message.plant, message.dp, message.value) == ("gw-0042", "p-17", "heating_curve_level", 24.0)
    assert message.expires_at > datetime.now(UTC)  # every command carries an expiry


def test_applied_result_bumps_version_and_writes_audit(setup):
    client, db = setup
    command_id = request(client, login(client, "op@a.example"), 24.0).json()["id"]
    s = store(db)
    s.publish_due(lambda gw, m: True, datetime.now(UTC))

    s.record_result(answer(command_id))

    detail = client.get("/v1/plants/p-17", headers=login(client, "op@a.example")).json()
    level = next(p for p in detail["parameters"] if p["datapoint"] == "heating_curve_level")
    history = client.get("/v1/plants/p-17/parameter-history", headers=login(client, "op@a.example")).json()
    assert (level["value"], level["version"]) == (24.0, 1)
    assert (history[0]["old_value"], history[0]["new_value"], history[0]["actor"]) == (26.0, 24.0, "op@a.example")


def test_s15_duplicate_answer_is_recorded_only_once(setup):
    client, db = setup
    command_id = request(client, login(client, "op@a.example"), 24.0).json()["id"]
    s = store(db)
    s.publish_due(lambda gw, m: True, datetime.now(UTC))

    first, second = s.record_result(answer(command_id)), s.record_result(answer(command_id))

    assert (first.accepted, second.accepted) == (True, False)
    assert count(db, "SELECT count(*) FROM parameter_audit") == 1
    assert count(db, "SELECT version FROM parameter") == 1


def test_s15_lost_answer_expires_then_a_late_answer_still_counts(setup):
    """No answer before the expiry -> 'expired'. If the gateway did apply it and its answer
    arrives late, that truth wins: the boiler really runs with the new value."""
    client, db = setup
    command_id = request(client, login(client, "op@a.example"), 24.0).json()["id"]
    s = store(db)
    s.publish_due(lambda gw, m: True, datetime.now(UTC))

    expired = s.expire(datetime.now(UTC) + timedelta(hours=1))
    late = s.record_result(answer(command_id))

    assert expired == 1
    assert late.state == "applied"


def test_broker_unreachable_keeps_the_command_waiting(setup):
    client, db = setup
    request(client, login(client, "op@a.example"), 24.0)

    sent = store(db).publish_due(lambda gw, m: False, datetime.now(UTC))

    assert sent == 0
    assert count(db, "SELECT count(*) FROM command WHERE state = 'requested'") == 1


def test_answer_from_a_foreign_gateway_is_ignored(setup):
    client, db = setup
    command_id = request(client, login(client, "op@a.example"), 24.0).json()["id"]

    outcome = store(db).record_result(answer(command_id, gateway="gw-evil"))

    assert outcome.accepted is False
    assert count(db, "SELECT count(*) FROM parameter_audit") == 0


def test_s16_second_change_while_the_first_is_open_is_a_conflict(setup):
    client, _ = setup
    op = login(client, "op@a.example")

    first, second = request(client, op, 24.0), request(client, op, 23.0)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["title"] == "Another change is still open"


def test_s16_change_based_on_an_old_version_is_a_conflict(setup):
    client, db = setup
    op = login(client, "op@a.example")
    command_id = request(client, op, 24.0).json()["id"]
    s = store(db)
    s.publish_due(lambda gw, m: True, datetime.now(UTC))
    s.record_result(answer(command_id))  # version is now 1

    stale = request(client, op, 22.0, version=0)  # this user still saw version 0

    assert stale.status_code == 409
    assert stale.json()["title"] == "Parameter changed in the meantime"


def test_four_eyes_big_change_needs_another_admin(setup):
    client, _ = setup
    created = request(client, login(client, "ad1@a.example"), 40.0).json()  # 26 -> 40: a big jump
    command_id = created["id"]

    own = client.post(f"/v1/commands/{command_id}/approve", headers=login(client, "ad1@a.example"))
    operator = client.post(f"/v1/commands/{command_id}/approve", headers=login(client, "op@a.example"))
    other = client.post(f"/v1/commands/{command_id}/approve", headers=login(client, "ad2@a.example"))

    assert created["state"] == "pending_approval"
    assert own.status_code == 403  # nobody approves their own request
    assert operator.status_code == 403  # operators may not approve at all
    assert other.json()["state"] == "requested"
    assert other.json()["approved_by"] == "ad2@a.example"


def test_a_viewer_cannot_change_parameters(setup):
    client, _ = setup
    viewer = login(client, "view@a.example")

    assert request(client, viewer, 27.0).status_code == 403
    assert client.get("/v1/plants/p-17", headers=viewer).status_code == 200  # reading is fine


def test_out_of_range_and_read_only_values_are_refused(setup):
    client, _ = setup
    op = login(client, "op@a.example")

    assert request(client, op, 99.0).status_code == 422
    assert request(client, op, 40.0, dp="flow_temp").status_code == 422


def test_audit_trail_cannot_be_edited_even_by_the_owner(setup):
    client, db = setup
    command_id = request(client, login(client, "op@a.example"), 24.0).json()["id"]
    s = store(db)
    s.publish_due(lambda gw, m: True, datetime.now(UTC))
    s.record_result(answer(command_id))

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), psycopg.connect(db) as conn:
        conn.execute("UPDATE parameter_audit SET new_value = 99")
