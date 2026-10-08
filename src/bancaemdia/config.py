import re
from functools import lru_cache
from ipaddress import ip_network

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_RATE_LIMIT_PATTERN = re.compile(
    r"^[1-9]\d*/(?:[1-9]\d*)?(?:second|minute|hour|day)s?$",
    re.IGNORECASE,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    STRIPE_SECRET_KEY: SecretStr | None = None
    STRIPE_WEBHOOK_SECRET: SecretStr | None = None
    BILLING_ENABLED: bool = False
    ONE_WIN_NATIVE_ENABLED: bool = False
    BILLING_CURRENCIES: str = "BRL"
    BILLING_RETURN_URL: str = "http://localhost:5173/assinatura"
    STRIPE_PORTAL_CONFIGURATION: str | None = None
    STRIPE_TEST_AUTOMATIC_TAX: bool = False

    DATABASE_URL: str = Field(..., description="Async DSN, e.g. postgresql+asyncpg://")
    DATABASE_URL_REPLICA: str = Field(
        ..., description="Read-replica DSN; same as primary in local dev"
    )
    DB_POOL_SIZE: int = Field(default=10, ge=1, description="Retained connections per API engine")
    DB_POOL_MAX_OVERFLOW: int = Field(
        default=20, ge=0, description="Temporary additional connections per API engine"
    )
    DB_POOL_PREWARM: bool = Field(
        default=False, description="Open retained API connections before startup completes"
    )
    DB_POOL_RECYCLE_SECONDS: int = Field(
        default=300, ge=1, description="Maximum age of an API connection before recycling"
    )
    REDIS_URL: str = Field(..., description="Redis connection URL")
    REDIS_CLUSTER_MODE: bool = Field(
        default=False, description="Use Redis Cluster for cache and Anthropic quota"
    )
    S3_UPLOAD_BUCKET: str | None = Field(
        default=None, description="Private bucket for uploaded review images"
    )
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
    TELEGRAM_BOT_TOKEN: str | None = Field(
        default=None, description="Bot API token; required when sending or polling"
    )
    TELEGRAM_WEBHOOK_SECRET: str | None = Field(
        default=None, description="Secret supplied to Telegram setWebhook"
    )
    TELEGRAM_MODE: str = Field(
        default="webhook", description="Telegram update transport: webhook or local polling"
    )
    TELEGRAM_API_BASE_URL: str = Field(
        default="https://api.telegram.org", description="Telegram Bot API origin"
    )
    TELEGRAM_ACTION_LIMITS: dict[str, tuple[int, int, int]] = Field(
        default={
            "link": (5, 5, 100),
            "photo": (10, 10, 200),
            "correction": (30, 30, 1000),
            "confirmation": (20, 20, 500),
        },
        description="Per action: chat, user, global counts; shared PostgreSQL windows",
    )
    TELEGRAM_LIMIT_WINDOW_SECONDS: int = Field(default=60, gt=0, le=86400)
    TELEGRAM_RAW_RETENTION_DAYS: int = Field(default=7, ge=1, le=365)
    TELEGRAM_MEDIA_RETENTION_DAYS: int = Field(default=7, ge=1, le=365)
    TELEGRAM_DRAFT_RETENTION_DAYS: int = Field(default=30, ge=1, le=365)
    TELEGRAM_PURGE_BATCH_SIZE: int = Field(default=100, gt=0, le=1000)

    @field_validator("TELEGRAM_ACTION_LIMITS")
    @classmethod
    def validate_telegram_limits(
        cls, value: dict[str, tuple[int, int, int]]
    ) -> dict[str, tuple[int, int, int]]:
        if set(value) != {"link", "photo", "correction", "confirmation"} or any(
            count <= 0 for limits in value.values() for count in limits
        ):
            raise ValueError("Telegram limits require all four actions and positive counts")
        return value

    COLETA_RATE_LIMIT: str = Field(
        default="10/minute", description="Coleta sends allowed per extension token"
    )
    COLETA_PAIRING_TTL_SECONDS: int = Field(default=600, ge=60, le=1800)
    COLETA_PAIRING_CREATE_LIMIT: int = Field(default=5, ge=1, le=100000)
    COLETA_PAIRING_EXCHANGE_LIMIT: int = Field(default=20, ge=1, le=100000)
    COLETA_PAIRING_GLOBAL_LIMIT: int = Field(default=300, ge=1, le=100000)
    COLETA_PAIRING_WINDOW_SECONDS: int = Field(default=60, ge=1, le=86400)
    COLETA_DAILY_LIMIT: int = Field(
        default=5000, ge=0, description="Raw house bets stored per user per day; 0 disables"
    )
    UPLOAD_MAX_BYTES: int = Field(
        default=50 * 1024 * 1024, gt=0, description="Largest Telegram export accepted, in bytes"
    )
    UPLOAD_DAILY_PHOTO_LIMIT: int = Field(
        default=200, ge=0, description="Photos per user per day sent to the AI; 0 disables"
    )
    UPLOAD_WEBHOOK_SECRET: str | None = Field(
        default=None, description="Shared secret the worker sends on /webhook/upload-complete"
    )
    API_INTERNAL_URL: str | None = Field(
        default=None, description="Base URL a worker uses to call this API back"
    )
    # Reserved for an issuer that does not exist yet. RS256 verification uses JWT_JWKS_URL,
    # never this secret; do not wire it into the verifier as a fallback.
    JWT_SECRET_KEY: str = Field(..., description="Reserved issuer signing key, not a verifier key")
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
    READINESS_CHECK_TIMEOUT_SECONDS: float = Field(
        default=2.0, gt=0, description="Timeout for each external readiness dependency"
    )
    CELERY_QUEUE_DEPTH_LIMIT: int = Field(
        default=1000,
        gt=0,
        description="Queue depth threshold reported as degraded by readiness telemetry",
    )
    RATE_LIMIT_STORAGE: str = Field(default="memory://", description="Rate-limit backend URL")
    RATE_LIMIT_STORAGE_TIMEOUT_SECONDS: float = Field(
        default=0.2,
        gt=0,
        le=2,
        description="Connect and command timeout for a Redis rate-limit backend",
    )
    RATE_LIMIT_TRUSTED_PROXY_CIDRS: str = Field(
        default="",
        description="Comma-separated networks whose forwarded client chain may be trusted",
    )
    API_RATE_LIMIT: str = Field(
        default="100/minute", description="Requests allowed across API v1 per authenticated user"
    )
    PANEL_EXPORT_RATE_LIMIT: str = Field(
        default="5/minute", description="Dashboard XLSX exports allowed per authenticated user"
    )
    AUTH_RATE_LIMIT: str = Field(
        default="20/minute", description="Requests allowed across authentication endpoints"
    )
    UPLOAD_RATE_LIMIT: str = Field(
        default="1/5minutes", description="Telegram upload requests allowed per user"
    )
    COLETA_IP_RATE_LIMIT: str = Field(
        default="60/minute",
        description="Coleta requests allowed per client across extension tokens",
    )
    STATEMENT_TIMEOUT_PRIMARY: int = Field(
        default=5000, description="Query timeout in ms for primary DB"
    )
    STATEMENT_TIMEOUT_REPLICA: int = Field(
        default=30000, description="Query timeout in ms for replica DB"
    )

    @field_validator(
        "COLETA_RATE_LIMIT",
        "API_RATE_LIMIT",
        "PANEL_EXPORT_RATE_LIMIT",
        "AUTH_RATE_LIMIT",
        "UPLOAD_RATE_LIMIT",
        "COLETA_IP_RATE_LIMIT",
    )
    @classmethod
    def validate_rate_limit(cls, value: str) -> str:
        if _RATE_LIMIT_PATTERN.fullmatch(value.strip()) is None:
            raise ValueError("rate limit must look like '100/minute' or '1/5minutes'")
        return value.strip().lower()

    @field_validator("RATE_LIMIT_TRUSTED_PROXY_CIDRS")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, value: str) -> str:
        networks = [part.strip() for part in value.split(",") if part.strip()]
        for network in networks:
            try:
                ip_network(network, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy network: {network}") from exc
        return ",".join(networks)

    @field_validator("TELEGRAM_MODE")
    @classmethod
    def validate_telegram_mode(cls, value: str) -> str:
        if value not in {"webhook", "polling"}:
            raise ValueError("TELEGRAM_MODE must be webhook or polling")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
