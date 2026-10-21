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

    batch_size: int = Field(default=200, ge=1, le=10_000)  # messages per DB write
    batch_max_wait_s: float = Field(default=1.0, gt=0)

    @property
    def database_url(self) -> str:
        password = quote(self.db_password.get_secret_value(), safe="")
        return f"postgresql://{self.db_user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
