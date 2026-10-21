# ADR 0010: Broker restarts — sessions on disk, gateways keep messages until confirmed

Status: accepted (2026-10-21). Found by the chaos test (S22).

## Context
The first chaos run restarted the broker while simulated gateways were sending:
**145 of 200 readings never arrived**. The investigation took three steps, and the
first explanation was wrong, so all three are written down.

1. **First suspicion: the gateways.** `client.publish()` returning success only
   means "queued inside the MQTT library". The MQTT 5 specification says a client
   that reconnects and gets "session present = 0" must discard its unconfirmed
   messages. So I built store-and-forward for the gateways (below). The loss stayed.
2. **A deadlock on the way.** The first version of that publisher held its own lock
   while calling `client.publish()`, while the library holds its internal lock when it
   calls our "confirmed" callback (lock-order inversion). Fixed by never calling the
   library while holding our lock.
3. **The real cause: the broker forgot the bridge.** Instrumenting the run showed the
   broker had confirmed all 200 readings to the gateways. But the test broker could not
   write its persistence file (wrong directory permissions), so after the restart it no
   longer knew the bridge's persistent session or subscription. Every reading published
   before the bridge reconnected and subscribed again had no subscriber, and the broker
   correctly dropped it, after confirming it to the gateway.

## Decision
- **The broker must keep sessions on disk.** `persistence true` with a writable volume
  (`mqtt-data:/mosquitto/data` in Docker Compose). The bridge connects with a persistent
  session (24 h), so while it is away the broker queues its messages (up to
  `max_queued_messages`). A test checks the config and the volume stay in place.
- **Gateways keep every message until its PUBACK** (`src/heatbeat/sim/publisher.py`,
  `ReliablePublisher`) and send unconfirmed ones again after a reconnect without a session.
  That covers what step 1 described for clients that follow the specification. (The
  paho library actually resends its own copy too; duplicates are harmless because the
  bridge stores each reading once.)

## Consequences
- After the fix the chaos test shows 0 lost and 0 duplicated readings for a bridge
  crash, a database outage and a broker restart (`docs/chaos-report.md`).
- A graceful broker restart (`docker compose stop`, SIGTERM) writes the session file.
  A hard crash loses what changed since the last autosave; in production a clustered or
  managed broker, or `autosave_interval` tuning, would address that.
- Real gateway firmware needs the same store-and-forward behaviour, plus a buffer on
  disk (flash) so a gateway reboot does not lose it; that is outside this project.
- Lesson: a test that measures end to end found a configuration error that every unit
  test would have missed, and the first plausible explanation was not the right one.
