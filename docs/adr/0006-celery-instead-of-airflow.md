# ADR 0006: Celery beat for all scheduled jobs; Airflow deferred

Status: accepted (2026-10-21). Supersedes the plan's "Airflow for data pipelines".

## Context
Two kinds of background work exist:
- event-driven, short, frequent: alarm evaluation (every minute), notification
  delivery with retries (every 10 s), command expiry (every 30 s)
- calendar-driven data jobs: weather forecast (hourly), prices (hourly), optimiser
  (daily), savings measurement (daily)
The original plan used Celery for the first kind and Airflow for the second.

## Options
1. Celery + Airflow (the plan): best tool for each job; two schedulers to run.
2. Celery beat for both.
3. Cron + plain scripts.

## Decision
Option 2, for now:
- Airflow does not run natively on Windows (the development machine), and it adds a
  scheduler, a webserver and a metadata database for four small daily/hourly jobs.
- Every job is a plain, idempotent function (`optimiser_jobs.py`); Celery only calls
  it. `uv run heatbeat-job <name>` runs any job by hand without Redis.
- Rollups need neither: TimescaleDB continuous aggregates and policies do them in
  the database.

## What would change my mind
- Jobs with dependencies between them (fetch -> clean -> optimise -> report).
- A need for backfills over past dates with run history (re-run "the optimiser as of
  1 March"). That is exactly what Airflow is good at; the job functions can be moved
  into DAGs unchanged.

## Consequences
- One fewer system; on Windows the worker uses Celery's `solo` pool (set automatically).
- Tasks are idempotent (database state, unique keys, SKIP LOCKED), so `acks_late`
  with redelivery after a worker crash is safe.
