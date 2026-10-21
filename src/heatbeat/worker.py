"""Background jobs (Celery + Redis) and their schedule.

What this file does: runs the event-driven, short jobs on a schedule:
alarms every minute, notifications every 10 s, command expiry every 30 s,
and the calendar jobs: weather (hourly), prices (hourly), optimiser (daily),
savings measurement (daily). Each task is a thin wrapper around a plain
function, so the logic is tested without Celery.
Why Celery beat and not Airflow: docs/adr/0006-celery-instead-of-airflow.md.

Run it (two terminals):
    uv run heatbeat-worker      (on Windows it uses the 'solo' pool automatically)
    uv run heatbeat-beat
"""

import logging
import os
import random
import tempfile
from datetime import UTC, datetime
from functools import cache

import psycopg
from celery import Celery
from celery.schedules import crontab
from pydantic_settings import BaseSettings, SettingsConfigDict

from heatbeat.alarm_engine import evaluate_alarms
from heatbeat.commands import CommandStore
from heatbeat.config import Settings
from heatbeat.external import OpenDataClient, SourceUnavailable
from heatbeat.notifier import Channel, EmailChannel, FakeSmsChannel, WebhookChannel, dispatch_due
from heatbeat.optimiser_jobs import fetch_prices, fetch_weather, measure_recommendations, run_optimiser

log = logging.getLogger("heatbeat.worker")


class _BrokerSettings(BaseSettings):
    """Only what Celery needs at import time (no password required here)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="HB_", extra="ignore")
    redis_url: str = "redis://127.0.0.1:6379/0"


app = Celery("heatbeat", broker=_BrokerSettings().redis_url)
app.conf.update(
    task_ignore_result=True,
    task_acks_late=True,  # a task lost with its worker is run again; tasks are idempotent
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    beat_schedule={
        "evaluate-alarms": {"task": "heatbeat.evaluate_alarms", "schedule": 60.0},
        "dispatch-notifications": {"task": "heatbeat.dispatch_notifications", "schedule": 10.0},
        "expire-commands": {"task": "heatbeat.expire_commands", "schedule": 30.0},
        "fetch-weather": {"task": "heatbeat.fetch_weather", "schedule": crontab(minute=7)},
        "fetch-prices": {"task": "heatbeat.fetch_prices", "schedule": crontab(minute=17)},
        "run-optimiser": {"task": "heatbeat.run_optimiser", "schedule": crontab(hour=5, minute=0)},
        "measure-recommendations": {
            "task": "heatbeat.measure_recommendations",
            "schedule": crontab(hour=6, minute=0),
        },
    },
)


@cache
def settings() -> Settings:
    return Settings()


@cache
def channels() -> dict[str, Channel]:
    s = settings()
    result: dict[str, Channel] = {
        "email": EmailChannel(s.smtp_host, s.smtp_port, s.mail_from),
        "sms": FakeSmsChannel(),
    }
    if s.webhook_url:
        result["webhook"] = WebhookChannel()
    return result


def connect() -> psycopg.Connection:
    return psycopg.connect(settings().database_url, autocommit=True)


def now() -> datetime:
    return datetime.now(UTC)


@app.task(name="heatbeat.evaluate_alarms")
def evaluate_alarms_task() -> None:
    with connect() as conn:
        evaluate_alarms(conn, settings(), now())


@app.task(name="heatbeat.dispatch_notifications")
def dispatch_notifications_task() -> None:
    with connect() as conn:
        dispatch_due(conn, channels(), now(), settings().notification_max_attempts, random.Random())


@app.task(name="heatbeat.expire_commands")
def expire_commands_task() -> None:
    with connect() as conn:
        CommandStore(conn).expire(now())


def _client() -> OpenDataClient:
    return OpenDataClient(settings().weather_url, settings().prices_url)


@app.task(name="heatbeat.fetch_weather")
def fetch_weather_task() -> None:
    with connect() as conn:
        log.info("weather: %s", fetch_weather(conn, _client(), now().date()))


@app.task(name="heatbeat.fetch_prices")
def fetch_prices_task() -> None:
    try:
        with connect() as conn:
            log.info("prices: %d hours stored", fetch_prices(conn, _client(), now().date()))
    except SourceUnavailable as exc:
        log.warning("prices unavailable: %s", exc)


@app.task(name="heatbeat.run_optimiser")
def run_optimiser_task() -> None:
    with connect() as conn:
        run_optimiser(conn, now())


@app.task(name="heatbeat.measure_recommendations")
def measure_recommendations_task() -> None:
    with connect() as conn:
        measure_recommendations(conn, now())


def worker_main() -> None:
    pool = ["--pool=solo"] if os.name == "nt" else ["--concurrency=2"]  # Celery has no prefork pool on Windows
    app.worker_main(["worker", "--loglevel=INFO", *pool])


def beat_main() -> None:
    schedule = os.path.join(tempfile.gettempdir(), "heatbeat-celerybeat-schedule")  # writable everywhere
    app.start(["beat", "--loglevel=INFO", f"--schedule={schedule}"])


def run_once_main() -> None:
    """Run one job by hand without Redis, e.g.:  uv run heatbeat-job optimiser"""
    import sys

    jobs = {
        "alarms": evaluate_alarms_task,
        "notify": dispatch_notifications_task,
        "expire": expire_commands_task,
        "weather": fetch_weather_task,
        "prices": fetch_prices_task,
        "optimiser": run_optimiser_task,
        "measure": measure_recommendations_task,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in jobs:
        sys.exit(f"usage: heatbeat-job {{{'|'.join(jobs)}}}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    jobs[sys.argv[1]]()
