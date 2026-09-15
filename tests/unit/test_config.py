from __future__ import annotations

import pytest
from pydantic import ValidationError

from bancaemdia.config import Settings, get_settings

REQUIRED = (
    "DATABASE_URL",
    "DATABASE_URL_REPLICA",
    "REDIS_URL",
    "JWT_SECRET_KEY",
    "JWT_AUDIENCE",
    "JWT_ISSUER",
    "APP_ENV",
)


@pytest.fixture(autouse=True)
def _isolate():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestSettingsLoadsFromDotenv:
    def test_acceptance_loads_from_env(self):
        s = get_settings()
        assert s.DATABASE_URL.startswith("postgresql+asyncpg")
        assert s.REDIS_URL.startswith("redis")


class TestSettingsValidation:
    def test_missing_required_raises(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL_REPLICA", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        monkeypatch.delenv("JWT_AUDIENCE", raising=False)
        monkeypatch.delenv("JWT_ISSUER", raising=False)
        monkeypatch.delenv("APP_ENV", raising=False)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


class TestAnthropicModelDefaults:
    def test_haiku_reads_and_sonnet_escalates(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_ESCALATION_MODEL", raising=False)
        s = Settings(_env_file=None)
        assert s.ANTHROPIC_MODEL == "claude-haiku-4-5"
        assert s.ANTHROPIC_ESCALATION_MODEL == "claude-sonnet-5"


class TestCelerySettingsDefaults:
    def test_default_to_compose_redis(self, monkeypatch):
        monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
        monkeypatch.delenv("CELERY_RESULT_BACKEND", raising=False)
        s = Settings(_env_file=None)
        assert s.CELERY_BROKER_URL == "redis://redis:6379/0"
        assert s.CELERY_RESULT_BACKEND == "redis://redis:6379/1"


class TestGetSettingsSingleton:
    def test_returns_same_instance(self):
        assert get_settings() is get_settings()
