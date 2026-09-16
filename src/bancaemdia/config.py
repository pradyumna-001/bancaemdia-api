from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = Field(..., description="Async DSN, e.g. postgresql+asyncpg://")
    DATABASE_URL_REPLICA: str = Field(
        ..., description="Read-replica DSN; same as primary in local dev"
    )
    REDIS_URL: str = Field(..., description="Redis connection URL")
    CELERY_BROKER_URL: str = Field(
        default="redis://redis:6379/0", description="Celery broker URL (Redis db 0)"
    )
    CELERY_RESULT_BACKEND: str = Field(
        default="redis://redis:6379/1", description="Celery result backend URL (Redis db 1)"
    )
    WORKER_METRICS_PORT: int | None = Field(
        default=None,
        description="Port where a Celery worker serves Prometheus metrics; off if unset",
    )
    ANTHROPIC_API_KEY: str | None = Field(default=None, description="Anthropic API key")
    ANTHROPIC_TIMEOUT: int = Field(
        default=30, description="HTTP timeout in seconds for Anthropic client"
    )
    ANTHROPIC_MAX_RETRIES: int = Field(default=3, description="Max retries on transient errors")
    ANTHROPIC_MODEL: str = Field(
        default="claude-haiku-4-5", description="Model for the first read of a betting slip"
    )
    ANTHROPIC_ESCALATION_MODEL: str = Field(
        default="claude-sonnet-5", description="Model for re-reading slips whose read did not check"
    )
    ANTHROPIC_RATE_USER: int = Field(
        default=10, gt=0, description="Anthropic requests allowed per user per window"
    )
    ANTHROPIC_RATE_GLOBAL: int = Field(
        default=100, gt=0, description="Anthropic requests allowed across all users per window"
    )
    ANTHROPIC_WINDOW: int = Field(
        default=60, gt=0, description="Anthropic rate-limit window in seconds"
    )
    COLETA_TOKEN_SECRET: str = Field(
        ..., description="HMAC key for the browser extension's coleta tokens"
    )
    COLETA_RATE_LIMIT: str = Field(
        default="10/minute", description="Coleta sends allowed per extension token"
    )
    COLETA_DAILY_LIMIT: int = Field(
        default=5000, ge=0, description="Raw house bets stored per user per day; 0 disables"
    )
    JWT_SECRET_KEY: str = Field(..., description="JWT signing secret key")
    JWT_ALGORITHM: str = Field(default="RS256", description="JWT signing algorithm")
    JWT_JWKS_URL: str | None = Field(
        default=None, description="JWKS endpoint for key rotation (RS256)"
    )
    JWT_AUDIENCE: str = Field(..., description="Expected JWT audience claim")
    JWT_ISSUER: str = Field(..., description="Expected JWT issuer claim")
    JWT_EXPIRY_MINUTES: int = Field(
        default=60, gt=0, description="Access token lifetime in minutes, for the token issuer"
    )
    APP_ENV: str = Field(..., description="Runtime environment: development | staging | production")
    LOG_LEVEL: str = Field(default="INFO", description="Structured log level")
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = Field(
        default=None, description="OpenTelemetry collector endpoint"
    )
    RATE_LIMIT_STORAGE: str = Field(default="memory://", description="Rate-limit backend URL")
    STATEMENT_TIMEOUT_PRIMARY: int = Field(
        default=5000, description="Query timeout in ms for primary DB"
    )
    STATEMENT_TIMEOUT_REPLICA: int = Field(
        default=30000, description="Query timeout in ms for replica DB"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
