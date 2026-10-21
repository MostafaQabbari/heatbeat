"""Settings, read from environment variables (or a local .env file)."""

from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HB_", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "heatbeat"
    db_user: str = "heatbeat"
    db_password: SecretStr  # no default: the app refuses to start without it

    mqtt_host: str = "localhost"
    mqtt_port: int = Field(default=1883, ge=1, le=65535)

    batch_size: int = Field(default=500, ge=1, le=10_000)
    batch_max_wait_s: float = Field(default=1.0, gt=0)

    @property
    def database_url(self) -> str:
        password = quote(self.db_password.get_secret_value(), safe="")
        return (
            f"postgresql://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )