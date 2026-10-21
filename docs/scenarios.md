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

## Added with tenants, alarms, commands, optimiser and operations

| Id | What goes wrong | Expected behaviour | Source | Tests |
|---|---|---|---|---|
| S12 | A query forgets its tenant filter | The database itself only shows the caller's tenant (RLS + filtering views); another tenant's plant gives the same 404 as a missing one | DDIA ch. 7 Multitenancy p. 254-255; ADR 0005 | `test_integration_security::test_s12_*` |
| S13 | A notification receiver is down | Retried with exponential backoff and jitter, parked as `dead` after the maximum attempts | DDIA ch. 8; SDI ch. 10 | `test_notifier::test_s13_*` |
| S14 | Crash between saving an alarm and sending the notification | Transactional outbox: alarm and notification written together; idempotency key makes a resend harmless | DDIA ch. 12 p. 507-508 | `test_integration_alarms::test_s14_*` |
| S15 | Command delivered twice, answer lost, answer late | Applied at most once; expired commands never applied; a late "applied" still counts | DDIA ch. 13 (end-to-end ids); ADR 0008 | `test_commands`, `test_gateway`, `test_integration_commands` (`test_s15_*`) |
| S16 | Two users change the same parameter | Version check and one-open-command index give 409 instead of a lost update | DDIA ch. 8 "Preventing Lost Updates" | `test_integration_commands::test_s16_*` |
| S17 | One tenant floods the API | Token bucket per tenant; 429 with `Retry-After`; other tenants unaffected | SDI ch. 4 (rate limiter) | `test_ratelimit::test_s17_*`, `test_api::test_s17_*` |
| S18 | A paused worker wakes up late / two workers evaluate the same rule | `FOR UPDATE SKIP LOCKED`; a held row is skipped, never evaluated twice | DDIA ch. 8 (process pauses) | `test_integration_alarms::test_s18_*` |
| S19 | Weather or price source is down or changes its format | Clear error, run marked `partial`, no recommendation without its input | Hard Parts (contracts); own design | `test_external`, `test_optimiser`, `test_integration_optimiser` (`test_s19_*`) |
| S20 | A savings claim that cannot be checked | Weather-normalised estimate with a 95 % range, tested against simulated data with a known saving | Own design (degree-day regression) | `test_savings::test_s20_*` |
| S21 | A gateway's clock drifts | Bridge measures offset per gateway, exports a metric, alert fires above 60 s | DDIA ch. 9 "Unreliable Clocks" | `test_observability::test_s21_*` |
| S22 | Bridge crash, database outage, broker restart while data flows | 0 readings lost, 0 duplicated | DDIA ch. 9 (faults); own chaos script | `scripts/chaos.py`, results in `chaos-report.md`; ADR 0010 |

## Planned (not built yet)

| Id | Scenario | Plan |
|---|---|---|
| S05 | Device clock jumps backwards | Flag readings older than the last one per gateway (the drift metric of S21 already shows large jumps) |
