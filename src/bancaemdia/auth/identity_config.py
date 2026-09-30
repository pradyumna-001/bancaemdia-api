"""Server-only identity configuration, independent of the production provider choice."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IdentitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    AUTH_ENABLED: bool = False
    AUTH_DATABASE_URL: SecretStr | None = None
    AUTH_PUBLIC_URL: str = ""
    AUTH_FRONTEND_ORIGIN: str = ""
    AUTH_COOKIE_SECURE: bool = True
    AUTH_SIGNING_KEYS_FILE: Path | None = None
    AUTH_ENCRYPTION_KEYS_FILE: Path | None = None
    AUTH_ACCESS_SECONDS: int = Field(default=300, ge=30, le=900)
    AUTH_SESSION_SECONDS: int = Field(default=604800, ge=300, le=2592000)
    AUTH_IDLE_SECONDS: int = Field(default=43200, ge=300, le=604800)
    OIDC_ISSUER: str = ""
    OIDC_CLIENT_ID: str = ""
    OIDC_CLIENT_SECRET: SecretStr | None = None
    OIDC_SCOPES: str = "openid email profile"
    OIDC_REVOCATION_ENDPOINT: str | None = None

    @model_validator(mode="after")
    def complete_configuration(self) -> "IdentitySettings":
        if not self.AUTH_ENABLED:
            return self
        if not all((
            self.AUTH_DATABASE_URL,
            self.AUTH_SIGNING_KEYS_FILE,
            self.AUTH_ENCRYPTION_KEYS_FILE,
            self.OIDC_CLIENT_ID,
        )):
            raise ValueError("Enabled identity requires database, key files and client ID")
        for url in (self.AUTH_PUBLIC_URL, self.AUTH_FRONTEND_ORIGIN, self.OIDC_ISSUER):
            parsed = urlsplit(url)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if not self.AUTH_COOKIE_SECURE and not local:
                raise ValueError("Insecure cookies are permitted only on loopback test hosts")
            if (
                not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.scheme not in {"https", "http"}
                or (parsed.scheme != "https" and (self.AUTH_COOKIE_SECURE or not local))
            ):
                raise ValueError("Identity URLs require HTTPS; insecure loopback is test-only")
        if urlsplit(self.AUTH_FRONTEND_ORIGIN).path not in {"", "/"}:
            raise ValueError("Frontend origin must not contain a path")
        if urlsplit(self.AUTH_PUBLIC_URL).path not in {"", "/"}:
            raise ValueError("Public API URL must be an origin")
        if urlsplit(self.AUTH_PUBLIC_URL).hostname != urlsplit(self.AUTH_FRONTEND_ORIGIN).hostname:
            raise ValueError(
                "Lax cookies require same-host frontend/API; use an origin reverse proxy"
            )
        if not {"openid", "email"} <= set(self.OIDC_SCOPES.split()):
            raise ValueError("OIDC requires openid and email scopes")
        return self

    @property
    def callback(self) -> str:
        return self.AUTH_PUBLIC_URL.rstrip("/") + "/auth/callback"

    @property
    def session_cookie(self) -> str:
        return "__Host-bancaemdia_session" if self.AUTH_COOKIE_SECURE else "bancaemdia_session"

    @property
    def flow_cookie(self) -> str:
        return "__Host-bancaemdia_flow" if self.AUTH_COOKIE_SECURE else "bancaemdia_flow"


@lru_cache
def identity_settings() -> IdentitySettings:
    return IdentitySettings()
