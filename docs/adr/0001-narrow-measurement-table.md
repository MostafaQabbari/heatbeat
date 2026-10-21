# ADR 0001: One narrow measurement table (TimescaleDB hypertable)

Status: accepted (2026-10-21)

## Context
Heating plants from different manufacturers expose different datapoints (a gas
boiler has burner starts, a heat pump has COP and defrost cycles). New device
types arrive all the time. Readings come in every 30 s per datapoint.

Design target (see README): 3,000 plants x 40 datapoints / 30 s = 4,000 rows/s.

## Options
1. **Wide table**: one column per datapoint. Fast to read one plant, but every new
   device type needs a migration, and most columns are NULL.
2. **JSONB per timestamp**: flexible, but no types, no per-datapoint plausibility
   limits, and weaker indexing and compression.
3. **Narrow table**: one row per (plant, datapoint, time, value), plus a `datapoint`
   catalogue with unit and plausible range.

## Decision
Option 3. `measurement(ts, plant_id, datapoint_id, value, quality)` as a hypertable
(1-day chunks), with `UNIQUE (plant_id, datapoint_id, ts)`.

- The unique key makes MQTT redelivery harmless (`ON CONFLICT DO NOTHING`), see ADR 0002.
- The same index serves the main query "one plant, some datapoints, a time range".
  A test (`test_main_query_uses_the_unique_index`) fails if that query turns into a
  sequential scan. Ford et al. call this kind of check an architecture
  *fitness function* (Software Architecture: The Hard Parts, ch. 1, p. 6-8).
- Compression: `segmentby = plant_id, datapoint_id`, `orderby = ts DESC`, after 7 days.
  Raw data is dropped after 365 days. `measurement_1h` is a continuous aggregate.

## Consequences
- More rows, and a join to resolve datapoint names. Mitigated by `smallint` ids and compression.
- A new datapoint is one `INSERT` into the catalogue, not a migration.
- Measured (docs/measurements.md): about 112 bytes/row uncompressed including the index,
  about 7 bytes/row compressed (ratio about 16x), on simulated data.
- Late data that lands in an already-compressed chunk is about 5x slower to write.
  That is why compression starts after 7 days, longer than any gateway buffers data.

Book: DDIA 2nd ed., ch. 4 "Storage and Retrieval" (column-oriented storage, write amplification).
