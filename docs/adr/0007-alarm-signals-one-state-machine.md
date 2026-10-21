# ADR 0007: Every alarm rule becomes a signal for ONE state machine

Status: accepted (2026-10-21)

## Context
Faults look different: a value too high, flow and return too close (hydraulics), a
burner switching on too often (short cycling), a sensor that never changes, a plant
that stopped sending. Writing one engine per fault type multiplies the logic that
must handle delays, hysteresis and late data.

## Decision
Each rule first derives a number over time, then one tested state machine decides
(normal -> pending -> firing -> normal):

| Rule kind | Signal |
|---|---|
| threshold | the value, or its rolling mean over a window (COP) |
| spread | value A minus value B at the same timestamp |
| rate | 0 -> 1 switches per hour in a window (burner starts) |
| flatline | max minus min in a window (stuck sensor) |
| absence | minutes since the plant last sent data |

The state machine fires only after `delay`, clears only past `limit -/+ hysteresis`,
and ignores samples older than the newest one it has seen (S09).

Evaluation runs every minute per (rule, plant) under `FOR UPDATE SKIP LOCKED`
(S18), reads only new samples up to now - 60 s (a watermark for data still in
flight), and writes state, history and notification outbox in one transaction (S14).

## How the default limits were chosen
Healthy and faulty simulated plants were measured (48 h, several seeds); limits sit in
the gap between them. A "fault matrix" test runs every plant type with every fault:
healthy plants raise nothing, every fault raises its alarm. It also showed a real
effect: a too-high heating curve lowers a heat pump's COP, so that plant raises both
"rooms overheated" and "COP low".

## Consequences
- New fault types are mostly a new signal function plus a test.
- Evaluation reads raw rows (exact, simple). At large scale it would read the
  one-minute aggregate or be split across workers (SKIP LOCKED already allows that).
- The plan's "pattern rule" (heating curve fitting) lives in the optimiser instead.

Sources: DDIA 2nd ed., ch. 12 (event time vs. processing time p. 519; transactional
outbox p. 507-508); own design.
