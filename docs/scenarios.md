# Failure scenarios

What this file does: lists what can go wrong, what should happen instead, where
the idea comes from, and which test proves it. Test names contain the scenario id.

Books: **DDIA** = Designing Data-Intensive Applications, 2nd ed. (Kleppmann, Riccomini).
**SDI** = System Design Interview Vol. 1 (Alex Xu). **Hard Parts** = Software
Architecture: The Hard Parts (Ford et al.).

## Covered by tests

| Id | What goes wrong | Expected behaviour | Source | Tests |
|---|---|---|---|---|
| S01 | Broker delivers a message twice (QoS 1 = at-least-once) | No duplicate rows | DDIA ch. 12 Idempotence p. 528; SDI ch. 10 dedupe | `test_integration_pipeline::test_s01_*`, `test_integration_storage::test_s01_*` (also in compressed chunks), `test_ingest::test_s01_*` |
| S02 | Database fails during a batch | Nothing acknowledged, nothing lost, no duplicates after recovery | DDIA ch. 12 | `test_bridge::test_s02_*`, `test_integration_storage::test_s02_*`, measured in measurements.md |
| S03 | Messages lost between gateway and cloud | Sequence gap detected and logged | DDIA ch. 12 | `test_ingest::test_s03_*` |
| S04 | Message acknowledged before it is stored | Ack only after commit; handlers set before connect | Own lesson | `test_bridge::test_s04_*` |
| S06 | Device clock in 1970 or in the future; time without timezone | Rejected with a reason | DDIA ch. 9 "Unreliable Clocks" p. 358-363 | `test_contract::test_s06_*`, `test_ingest::test_s06_*` |
| S07 | Gateway reboots, `seq` restarts at 1 | Recognised by new `boot_id` | DDIA ch. 10 (logical clocks) | `test_ingest::test_s07_*` |
| S08 | Many gateways upload hours of backlog at once | Drains in measured time | DDIA ch. 2 (percentiles, overload); SDI ch. 2 | measurements.md |
| S09 | Late data arrives after a decision | Older samples never change the alarm state | DDIA ch. 12 "Event time versus processing time" p. 519 | `test_alarms::test_s09_*`, property test |
| S10 | New firmware sends extra fields | Old reader ignores them | DDIA ch. 5 "Encoding and Evolution" p. 162, 171 | `test_contract::test_s10_*` |
| S11 | Migration must be reversible | Migrations go down and up cleanly | DDIA ch. 5 | `test_integration_storage::test_s11_*` |
| S13 | Retries in lockstep overload a recovering system | Exponential backoff with jitter and a cap | DDIA ch. 8; SDI ch. 10 | `test_bridge::test_s13_*` |

## Also tested (own design, security and robustness)

| What goes wrong | Expected behaviour | Tests |
|---|---|---|
| Gateway publishes data claiming to be another gateway | Rejected (topic must match payload) | `test_ingest::test_gateway_cannot_speak_for_another_gateway` |
| Gateway writes to a plant that belongs to another gateway | Rejected, stored with reason | `test_integration_storage::test_plant_cannot_be_taken_over_*` |
| Garbage message ("poison") | Parked in `rejected_message`, does not block others | `test_integration_pipeline::test_garbage_*` |
| Sensor reports NaN / infinity | Rejected | `test_contract::test_broken_sensor_values_*` |
| Sensor reports an impossible value (500 degC) | Stored, flagged `quality = 2`, excluded from aggregates | `test_ingest`, `test_integration_storage` |
| Huge time range or tiny bucket in an API query | 422 before the database is asked | `test_api::test_series_limits_protect_the_database` |
| Main query turns into a full scan | Test fails (fitness function, Hard Parts ch. 1) | `test_integration_storage::test_main_query_uses_the_unique_index` |
| API code and documentation drift apart | Test fails | `test_openapi.py` |

## Planned (not built yet)

| Id | Scenario | Plan |
|---|---|---|
| S05 | Device clock jumps backwards | Flag readings older than the last one per gateway |
| S12 | A query forgets its tenant filter | PostgreSQL row-level security (DDIA ch. 7 Multitenancy p. 254-255) |
| S14 | Crash between saving an alarm and sending the notification | Transactional outbox (DDIA ch. 12 p. 507-508) |
| S15, S16 | Lost command acknowledgement; two users change one parameter | Command ids, expiry, optimistic version check |
| S17 | One tenant floods the API | Per-tenant rate limit (SDI ch. 4) |
| S18 | Paused worker wakes up late | Row locking with `FOR UPDATE SKIP LOCKED` |
