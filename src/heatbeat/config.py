"""Settings, read from environment variables (or a local .env file).

What this file does: one place for every setting (database, MQTT, batching).
Secrets come from the environment, never from code.
"""

from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HB_", extra="ignore")

    # 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first and
    # waits about 2 s per connection, because Docker only listens on IPv4.
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "heatbeat"
    db_user: str = "heatbeat"
    db_password: SecretStr  # no default: the app refuses to start without it

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_client_id: str = "heatbeat-bridge"

    api_host: str = "127.0.0.1"  # only this computer; Docker sets 0.0.0.0
    api_port: int = Field(default=8000, ge=1, le=65535)
    jwt_secret: SecretStr | None = None  # required by the API, at least 32 characters
    jwt_ttl_minutes: int = Field(default=60, ge=1, le=24 * 60)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]  # Vue dev server
    rate_limit_per_minute: int = Field(default=600, ge=1)

    batch_size: int = Field(default=200, ge=1, le=10_000)  # messages per DB write
    batch_max_wait_s: float = Field(default=1.0, gt=0)
    bridge_metrics_port: int = Field(default=9101, ge=1, le=65535)

    redis_url: str = "redis://127.0.0.1:6379/0"  # Celery broker
    smtp_host: str = "127.0.0.1"  # Mailpit in development
    smtp_port: int = 1025
    mail_from: str = "alarms@heatbeat.local"
    alarm_email_to: str = "operations@heatbeat.local"
    webhook_url: str | None = None  # e.g. a ticket system; None = channel off
    notification_max_attempts: int = Field(default=8, ge=1)

    command_ttl_s: int = Field(default=300, ge=10)  # a command not applied in time expires
    approval_threshold: float = Field(default=5.0, ge=0)  # bigger changes need a second person

    weather_url: str = "https://api.brightsky.dev"
    prices_url: str = "https://api.energy-charts.info"

    @property
    def database_url(self) -> str:
        password = quote(self.db_password.get_secret_value(), safe="")
        return f"postgresql://{self.db_user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
