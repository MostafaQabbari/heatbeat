# ADR 0008: Remote parameter changes through a defensive command pipeline

Status: accepted (2026-10-21)

## Context
Changing a parameter of a real heating plant from the cloud is the riskiest thing
the platform does. Messages can be lost, duplicated or delayed, gateways can be
offline for hours, two people can edit the same value, and a bug could send a
dangerous value.

## Decision
A change is a `command` row with a state machine, never a direct write:

1. **Validation twice**: the API checks "writable" and the allowed range from the
   datapoint catalogue; the gateway checks its own copy again (defence in depth).
2. **Optimistic concurrency** (S16): the request carries the parameter version the user
   saw; a different current version gives 409. A partial unique index allows only one
   open command per plant and parameter, so concurrent requests also get 409.
3. **Four-eyes**: big changes (more than the threshold) and switching heating off wait
   in `pending_approval`; the approver must be a different person (database CHECK too).
4. **The command table is the outbox**: the bridge publishes `requested` commands
   (FOR UPDATE SKIP LOCKED), QoS 1, never retained, with a `command_id` and an expiry.
5. **Gateway rules** (S15): never apply an expired command; apply each `command_id` at
   most once and answer a duplicate with the same result.
6. **Results**: duplicate results are ignored; a late "applied" after expiry is accepted
   (the gateway is the truth about the boiler); "applied" bumps the version and writes
   the audit row in the same transaction.
7. **Audit trail** is append-only: a trigger blocks UPDATE and DELETE for every role.

## Consequences
- Tested at every layer: pure state machine (property test "applied at most once"),
  gateway unit tests, database integration tests, and an end-to-end test where a
  command travels API row -> bridge -> MQTT -> simulated gateway -> result -> audit.
- Optimiser recommendations reuse the same path, so automation cannot bypass any check.
- Not built: an alarm when a command expires (it is visible in the API and as the
  metric `heatbeat_commands_expired_24h` with an alert).

Sources: DDIA 2nd ed., ch. 13 (end-to-end request ids); ch. 9 (last-write-wins loses
updates); own design.
