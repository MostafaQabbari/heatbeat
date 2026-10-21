# Query plans

What this file does: shows how PostgreSQL/TimescaleDB executes the API's hot queries,
run as the restricted API role through the tenant-filtering views (ADR 0005).
Generated 2026-10-21 13:29 UTC by `scripts/query_plans.py`; 1,980,583 measurement rows; TimescaleDB 2.17.2; plant `p-0001`.

What to look for: chunk exclusion (only recent chunks appear), index scans on
the `measurement_unique` key (plant_id, datapoint_id, ts) that also makes writes idempotent,
and the hourly view reading the continuous aggregate instead of raw rows.

Findings while writing this file:
- The first version of the latest-value query joined `plant` by key. Through the
  security-barrier view that condition did not reach the index, so it read the last day
  of ALL the tenant's plants. Filtering on `plant_id` lets it use the unique index
  (compare the first two plans). It still sorts one day of one plant; a per-datapoint
  `LATERAL ... ORDER BY ts DESC LIMIT 1` was tried and was not faster through the view.
- PostgreSQL's JIT compiler is on by default and once added about 0.4 s to a 20 ms query
  whose cost estimate was high. For short API queries `jit = off` for the API role is worth
  measuring.
- The hourly view is a real-time continuous aggregate: materialized hours plus raw rows
  since the last refresh (`materialized_only = false`), hence the UNION in its plan.

## Latest value per datapoint, FIRST version: filter through a join on plant.key

```sql
        SELECT DISTINCT ON (d.key) d.key, m.ts, m.value
        FROM measurement_visible m
        JOIN plant p ON p.id = m.plant_id
        JOIN datapoint d ON d.id = m.datapoint_id
        WHERE p.key = %(plant)s AND m.ts > now() - INTERVAL '1 day'
        ORDER BY d.key, m.ts DESC
```

```
Unique (actual time=237.769..245.694 rows=10 loops=1)
  Buffers: shared hit=75524
  ->  Sort (actual time=237.766..241.032 rows=36250 loops=1)
        Sort Key: d.key, m.ts DESC
        Sort Method: quicksort  Memory: 3261kB
        Buffers: shared hit=75524
        ->  Nested Loop (actual time=0.084..191.147 rows=36250 loops=1)
              Buffers: shared hit=75518
              ->  Hash Join (actual time=0.062..159.299 rows=36250 loops=1)
                    Hash Cond: (m.plant_id = p.id)
                    Buffers: shared hit=3018
                    ->  Hash Join (actual time=0.045..143.801 rows=186679 loops=1)
                          Hash Cond: (m.plant_id = p_1.id)
                          Buffers: shared hit=3016
                          ->  Custom Scan (ChunkAppend) on measurement m (actual time=0.007..103.351 rows=344737 loops=1)
                                Chunks excluded during startup: 0
                                Buffers: shared hit=3014
                                ->  Seq Scan on _hyper_8_40_chunk m_1 (actual time=0.006..59.454 rows=225575 loops=1)
                                      Filter: (ts > (now() - '1 day'::interval))
                                      Buffers: shared hit=1880
                                ->  Bitmap Heap Scan on _hyper_8_48_chunk m_2 (actual time=2.937..21.439 rows=119162 loops=1)
                                      Recheck Cond: (ts > (now() - '1 day'::interval))
                                      Heap Blocks: exact=1001
                                      Buffers: shared hit=1134
                                      ->  Bitmap Index Scan on _hyper_8_48_chunk_measurement_ts_idx (actual time=2.801..2.801 rows=119162 loops=1)
                                            Index Cond: (ts > (now() - '1 day'::interval))
                                            Buffers: shared hit=133
                          ->  Hash (actual time=0.030..0.032 rows=5 loops=1)
                                Buckets: 1024  Batches: 1  Memory Usage: 9kB
                                Buffers: shared hit=2
                                ->  Seq Scan on plant p_1 (actual time=0.011..0.023 rows=5 loops=1)
                                      Filter: (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer)
                                      Rows Removed by Filter: 74
                                      Buffers: shared hit=2
                    ->  Hash (actual time=0.010..0.010 rows=1 loops=1)
                          Buckets: 1024  Batches: 1  Memory Usage: 9kB
                          Buffers: shared hit=2
                          ->  Seq Scan on plant p (actual time=0.003..0.008 rows=1 loops=1)
                                Filter: ((key = 'p-0001'::text) AND (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer))
                                Rows Removed by Filter: 78
                                Buffers: shared hit=2
              ->  Index Scan using datapoint_pkey on datapoint d (actual time=0.001..0.001 rows=1 loops=36250)
                    Index Cond: (id = m.datapoint_id)
                    Buffers: shared hit=72500
Planning:
  Buffers: shared hit=393
Planning Time: 1.129 ms
Execution Time: 246.089 ms
```

## Latest value per datapoint, CURRENT version: filter on plant_id (plant detail page)

```sql
        SELECT DISTINCT ON (d.key) d.key, m.ts, m.value
        FROM measurement_visible m
        JOIN datapoint d ON d.id = m.datapoint_id
        WHERE m.plant_id = (SELECT id FROM plant WHERE key = %(plant)s)
          AND m.ts > now() - INTERVAL '1 day'
        ORDER BY d.key, m.ts DESC
```

```
Unique (actual time=82.418..89.312 rows=10 loops=1)
  Buffers: shared hit=568
  InitPlan 1 (returns $0)
    ->  Seq Scan on plant (actual time=0.009..0.022 rows=1 loops=1)
          Filter: ((key = 'p-0001'::text) AND (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer))
          Rows Removed by Filter: 78
          Buffers: shared hit=2
  ->  Sort (actual time=82.416..85.160 rows=36250 loops=1)
        Sort Key: d.key, m.ts DESC
        Sort Method: quicksort  Memory: 3261kB
        Buffers: shared hit=568
        ->  Hash Join (actual time=2.245..30.731 rows=36250 loops=1)
              Hash Cond: (m.datapoint_id = d.id)
              Buffers: shared hit=568
              ->  Nested Loop (actual time=2.209..21.257 rows=36250 loops=1)
                    Buffers: shared hit=567
                    ->  Seq Scan on plant p (actual time=0.034..0.063 rows=1 loops=1)
                          Filter: ((id = $0) AND (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer))
                          Rows Removed by Filter: 78
                          Buffers: shared hit=4
                    ->  Custom Scan (ChunkAppend) on measurement m (actual time=2.171..16.070 rows=36250 loops=1)
                          Chunks excluded during startup: 0
                          Hypertables excluded during runtime: 0
                          Buffers: shared hit=563
                          ->  Bitmap Heap Scan on _hyper_8_40_chunk m_1 (actual time=2.153..7.812 rows=23650 loops=1)
                                Recheck Cond: ((plant_id = $0) AND (ts > (now() - '1 day'::interval)))
                                Heap Blocks: exact=218
                                Buffers: shared hit=342
                                ->  Bitmap Index Scan on "40_15_measurement_unique" (actual time=2.116..2.116 rows=23650 loops=1)
                                      Index Cond: ((plant_id = $0) AND (ts > (now() - '1 day'::interval)))
                                      Buffers: shared hit=124
                          ->  Bitmap Heap Scan on _hyper_8_48_chunk m_2 (actual time=1.547..4.343 rows=12600 loops=1)
                                Recheck Cond: ((plant_id = $0) AND (ts > (now() - '1 day'::interval)))
                                Heap Blocks: exact=105
                                Buffers: shared hit=221
                                ->  Bitmap Index Scan on "48_36_measurement_unique" (actual time=1.510..1.510 rows=12600 loops=1)
                                      Index Cond: ((plant_id = $0) AND (ts > (now() - '1 day'::interval)))
                                      Buffers: shared hit=116
              ->  Hash (actual time=0.029..0.031 rows=18 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 9kB
                    Buffers: shared hit=1
                    ->  Seq Scan on datapoint d (actual time=0.014..0.018 rows=18 loops=1)
                          Buffers: shared hit=1
Planning:
  Buffers: shared hit=39
Planning Time: 0.870 ms
Execution Time: 89.736 ms
```

## Raw series, 6 hours in 5-minute buckets (trend chart, short range)

```sql
        SELECT d.key, time_bucket('5 minutes', m.ts) AS ts, avg(m.value), min(m.value), max(m.value)
        FROM measurement_visible m
        JOIN plant p ON p.id = m.plant_id
        JOIN datapoint d ON d.id = m.datapoint_id
        WHERE p.key = %(plant)s AND d.key = ANY(%(dps)s)
          AND m.ts >= now() - INTERVAL '6 hours' AND m.ts < now() AND m.quality = 0
        GROUP BY 1, 2 ORDER BY 1, 2
```

```
GroupAggregate (actual time=47.693..51.736 rows=216 loops=1)
  Group Key: d.key, (time_bucket('00:05:00'::interval, m.ts))
  Buffers: shared hit=1331
  ->  Incremental Sort (actual time=47.680..51.150 rows=4035 loops=1)
        Sort Key: d.key, (time_bucket('00:05:00'::interval, m.ts))
        Presorted Key: d.key
        Full-sort Groups: 3  Sort Method: quicksort  Average Memory: 28kB  Peak Memory: 28kB
        Pre-sorted Groups: 3  Sort Method: quicksort  Average Memory: 122kB  Peak Memory: 122kB
        Buffers: shared hit=1331
        ->  Nested Loop (actual time=1.774..49.938 rows=4035 loops=1)
              Join Filter: (d.id = m.datapoint_id)
              Rows Removed by Join Filter: 36315
              Buffers: shared hit=1331
              ->  Index Scan using datapoint_key_key on datapoint d (actual time=0.025..0.044 rows=3 loops=1)
                    Index Cond: (key = ANY ('{flow_temp,return_temp,outdoor_temp}'::text[]))
                    Buffers: shared hit=7
              ->  Materialize (actual time=0.581..15.457 rows=13450 loops=3)
                    Buffers: shared hit=1324
                    ->  Hash Join (actual time=1.739..39.682 rows=13450 loops=1)
                          Hash Cond: (m.plant_id = p.id)
                          Buffers: shared hit=1324
                          ->  Nested Loop (actual time=1.663..32.550 rows=69187 loops=1)
                                Buffers: shared hit=1322
                                ->  Seq Scan on plant p_1 (actual time=0.012..0.068 rows=5 loops=1)
                                      Filter: (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer)
                                      Rows Removed by Filter: 74
                                      Buffers: shared hit=2
                                ->  Custom Scan (ChunkAppend) on measurement m (actual time=1.382..4.938 rows=13837 loops=5)
                                      Chunks excluded during startup: 0
                                      Buffers: shared hit=1320
                                      ->  Bitmap Heap Scan on _hyper_8_40_chunk m_1 (actual time=1.381..3.903 rows=13837 loops=5)
                                            Recheck Cond: ((plant_id = p_1.id) AND (ts >= (now() - '06:00:00'::interval)) AND (ts < now()))
                                            Filter: (quality = 0)
                                            Heap Blocks: exact=685
                                            Buffers: shared hit=1320
                                            ->  Bitmap Index Scan on "40_15_measurement_unique" (actual time=1.356..1.356 rows=13837 loops=5)
                                                  Index Cond: ((plant_id = p_1.id) AND (ts >= (now() - '06:00:00'::interval)) AND (ts < now()))
                                                  Buffers: shared hit=635
                          ->  Hash (actual time=0.068..0.069 rows=1 loops=1)
                                Buckets: 1024  Batches: 1  Memory Usage: 9kB
                                Buffers: shared hit=2
                                ->  Seq Scan on plant p (actual time=0.051..0.064 rows=1 loops=1)
                                      Filter: ((key = 'p-0001'::text) AND (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer))
                                      Rows Removed by Filter: 78
                                      Buffers: shared hit=2
Planning:
  Buffers: shared hit=57
Planning Time: 0.985 ms
Execution Time: 52.006 ms
```

## Hourly aggregate, 7 days (trend chart, long range)

```sql
        SELECT d.key, c.bucket, c.avg_value, c.min_value, c.max_value
        FROM measurement_1h_visible c
        JOIN plant p ON p.id = c.plant_id
        JOIN datapoint d ON d.id = c.datapoint_id
        WHERE p.key = %(plant)s AND d.key = ANY(%(dps)s)
          AND c.bucket >= now() - INTERVAL '7 days' AND c.bucket < now()
        ORDER BY 1, 2
```

```
Incremental Sort (actual time=35.615..36.082 rows=504 loops=1)
  Sort Key: d.key, "*SELECT* 1".bucket
  Presorted Key: d.key
  Full-sort Groups: 3  Sort Method: quicksort  Average Memory: 29kB  Peak Memory: 29kB
  Pre-sorted Groups: 3  Sort Method: quicksort  Average Memory: 36kB  Peak Memory: 36kB
  Buffers: shared hit=439
  ->  Nested Loop (actual time=0.126..35.844 rows=504 loops=1)
        Join Filter: (d.id = "*SELECT* 1".datapoint_id)
        Rows Removed by Join Filter: 4536
        Buffers: shared hit=439
        ->  Index Scan using datapoint_key_key on datapoint d (actual time=0.006..0.020 rows=3 loops=1)
              Index Cond: (key = ANY ('{flow_temp,return_temp,outdoor_temp}'::text[]))
              Buffers: shared hit=4
        ->  Materialize (actual time=0.018..11.821 rows=1680 loops=3)
              Buffers: shared hit=435
              ->  Nested Loop (actual time=0.053..34.916 rows=1680 loops=1)
                    Join Filter: (p.id = "*SELECT* 1".plant_id)
                    Rows Removed by Join Filter: 7056
                    Buffers: shared hit=435
                    ->  Seq Scan on plant p (actual time=0.007..0.022 rows=1 loops=1)
                          Filter: ((key = 'p-0001'::text) AND (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer))
                          Rows Removed by Filter: 78
                          Buffers: shared hit=2
                    ->  Hash Join (actual time=0.034..34.264 rows=8736 loops=1)
                          Hash Cond: ("*SELECT* 1".plant_id = p_1.id)
                          Buffers: shared hit=433
                          ->  Append (actual time=0.010..32.448 rows=16198 loops=1)
                                Buffers: shared hit=431
                                ->  Subquery Scan on "*SELECT* 1" (actual time=0.010..7.841 rows=15936 loops=1)
                                      Buffers: shared hit=155
                                      ->  Result (actual time=0.009..6.526 rows=15936 loops=1)
                                            Buffers: shared hit=155
                                            ->  Custom Scan (ChunkAppend) on _materialized_hypertable_11 (actual time=0.009..5.172 rows=15936 loops=1)
                                                  Chunks excluded during startup: 0
                                                  Buffers: shared hit=155
                                                  ->  Seq Scan on _hyper_11_41_chunk (actual time=0.008..4.250 rows=15936 loops=1)
                                                        Filter: ((bucket < '2026-10-21 14:00:00+02'::timestamp with time zone) AND (bucket < now()) AND (bucket >= (now() - '7 days'::interval)))
                                                        Rows Removed by Filter: 96
                                                        Buffers: shared hit=155
                                ->  Subquery Scan on "*SELECT* 2" (actual time=19.309..23.724 rows=262 loops=1)
                                      Buffers: shared hit=276
                                      ->  GroupAggregate (actual time=19.308..23.698 rows=262 loops=1)
                                            Group Key: (time_bucket('01:00:00'::interval, measurement.ts)), measurement.plant_id, measurement.datapoint_id
                                            Buffers: shared hit=276
                                            ->  Sort (actual time=19.261..20.471 rows=26699 loops=1)
                                                  Sort Key: (time_bucket('01:00:00'::interval, measurement.ts)), measurement.plant_id, measurement.datapoint_id
                                                  Sort Method: quicksort  Memory: 2020kB
                                                  Buffers: shared hit=276
                                                  ->  Result (actual time=0.533..11.965 rows=26699 loops=1)
                                                        Buffers: shared hit=270
                                                        ->  Custom Scan (ChunkAppend) on measurement (actual time=0.531..9.571 rows=26699 loops=1)
                                                              Chunks excluded during startup: 0
                                                              Buffers: shared hit=270
                                                              ->  Bitmap Heap Scan on _hyper_8_40_chunk (actual time=0.530..8.057 rows=26699 loops=1)
                                                                    Recheck Cond: (ts >= '2026-10-21 14:00:00+02'::timestamp with time zone)
                                                                    Filter: ((quality = 0) AND (time_bucket('01:00:00'::interval, ts) < now()) AND (time_bucket('01:00:00'::interval, ts) >= (now() - '7 days'::interval)))
                                                                    Heap Blocks: exact=238
                                                                    Buffers: shared hit=270
                                                                    ->  Bitmap Index Scan on _hyper_8_40_chunk_measurement_ts_idx (actual time=0.497..0.497 rows=26699 loops=1)
                                                                          Index Cond: (ts >= '2026-10-21 14:00:00+02'::timestamp with time zone)
                                                                          Buffers: shared hit=32
                          ->  Hash (actual time=0.020..0.021 rows=5 loops=1)
                                Buckets: 1024  Batches: 1  Memory Usage: 9kB
                                Buffers: shared hit=2
                                ->  Seq Scan on plant p_1 (actual time=0.006..0.018 rows=5 loops=1)
                                      Filter: (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer)
                                      Rows Removed by Filter: 74
                                      Buffers: shared hit=2
Planning:
  Buffers: shared hit=295
Planning Time: 1.171 ms
Execution Time: 36.202 ms
```
