"""Tests for settings (src/heatbeat/config.py)."""

import pytest
from pydantic import ValidationError

from heatbeat.config import Settings


def test_missing_password_fails_fast(monkeypatch):
    monkeypatch.delenv("HB_DB_PASSWORD", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_password_is_hidden_when_printed(monkeypatch):
    monkeypatch.setenv("HB_DB_PASSWORD", "s3cret")

    settings = Settings(_env_file=None)

    assert "s3cret" not in repr(settings)
    assert "s3cret" in settings.database_url


def test_environment_overrides_default(monkeypatch):
    monkeypatch.setenv("HB_DB_PASSWORD", "x")
    monkeypatch.setenv("HB_MQTT_PORT", "8883")

    assert Settings(_env_file=None).mqtt_port == 8883