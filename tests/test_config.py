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


def test_special_characters_in_password_are_escaped(monkeypatch):
    monkeypatch.setenv("HB_DB_PASSWORD", "p@ss/word")

    assert "p%40ss%2Fword@" in Settings(_env_file=None).database_url


def test_api_listens_only_locally_by_default(monkeypatch):
    monkeypatch.setenv("HB_DB_PASSWORD", "x")

    assert Settings(_env_file=None).api_host == "127.0.0.1"
