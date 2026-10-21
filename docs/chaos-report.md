# Chaos report (S22)

Measured 2026-10-21 13:27 UTC. Environment: one Linux cloud VM, local processes (PostgreSQL 16 + TimescaleDB 2.17.2, Mosquitto 2.0.18 with persistence).
Load: 5 gateways x 1 message/s with store-and-forward; each service down for 20 s while the load continues. Re-run with `uv run python scripts/chaos.py`.

| Scenario | Readings produced | Stored | Lost | Duplicates | Re-sent by gateways | All stored after |
|---|---|---|---|---|---|---|
| bridge-crash | 200 | 200 | 0 | 0 | 0 | 0.0 s |
| database-down | 205 | 205 | 0 | 0 | 0 | 0.8 s |
| broker-restart | 200 | 200 | 0 | 0 | 0 | 2.1 s |

History: the first run lost 145 of 200 readings in broker-restart. Cause: the test broker
could not write its session file, so after the restart it had forgotten the bridge's
subscription. Details and the other two findings on the way (a deadlock, a wrong first
explanation) in docs/adr/0010-gateway-store-and-forward.md.
