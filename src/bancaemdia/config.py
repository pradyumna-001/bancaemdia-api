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
    DATABASE_URL_REPLICA: str = Field(..., description="Read-replica DSN; same as primary in local dev")
    REDIS_URL: str = Field(..., description="Redis connection URL")
    ANTHROPIC_API_KEY: str | None = Field(default=None, description="Anthropic API key")
    ANTHROPIC_TIMEOUT: int = Field(default=30, description="HTTP timeout in seconds for Anthropic client")
    ANTHROPIC_MAX_RETRIES: int = Field(default=3, description="Max retries on transient errors")
    JWT_SECRET_KEY: str = Field(..., description="JWT signing secret key")
    JWT_ALGORITHM: str = Field(default="RS256", description="JWT signing algorithm")
    JWT_JWKS_URL: str | None = Field(default=None, description="JWKS endpoint for key rotation (RS256)")
    JWT_AUDIENCE: str = Field(..., description="Expected JWT audience claim")
    JWT_ISSUER: str = Field(..., description="Expected JWT issuer claim")
    APP_ENV: str = Field(..., description="Runtime environment: development | staging | production")
    LOG_LEVEL: str = Field(default="INFO", description="Structured log level")
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = Field(default=None, description="OpenTelemetry collector endpoint")
    RATE_LIMIT_STORAGE: str = Field(default="memory://", description="Rate-limit backend URL")
    STATEMENT_TIMEOUT_PRIMARY: int = Field(default=5000, description="Query timeout in ms for primary DB")
    STATEMENT_TIMEOUT_REPLICA: int = Field(default=30000, description="Query timeout in ms for replica DB")


@lru_cache
def get_settings() -> Settings:
    return Settings()
