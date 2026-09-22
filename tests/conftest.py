from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost:5432/bancaemdia",
)
os.environ.setdefault(
    "DATABASE_URL_REPLICA", "postgresql+asyncpg://user:password@localhost:5432/bancaemdia"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "TEST_SECRET_KEY_REPLACE_IN_PRODUCTION")
os.environ.setdefault("JWT_SECRET_KEY", "TEST_JWT_SECRET")
os.environ.setdefault("COLETA_TOKEN_SECRET", "TEST_COLETA_TOKEN_SECRET")
os.environ.setdefault("UPLOAD_WEBHOOK_SECRET", "TEST_UPLOAD_WEBHOOK_SECRET")
os.environ.setdefault("API_INTERNAL_URL", "http://api.test")
os.environ.setdefault("JWT_AUDIENCE", "test")
os.environ.setdefault("JWT_ISSUER", "test")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "INFO")
# Tests reset buckets between cases and must never point that reset at a shared Redis instance.
os.environ["RATE_LIMIT_STORAGE"] = "memory://"
os.environ.setdefault("STATEMENT_TIMEOUT_PRIMARY", "5000")
os.environ.setdefault("STATEMENT_TIMEOUT_REPLICA", "30000")


@pytest.fixture(autouse=True)
def _isolate_api_rate_limit_buckets():
    # Production buckets deliberately outlive requests. Tests must not depend on execution order
    # or on which xdist worker happened to run a previous request with the same user.
    from bancaemdia.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()
    yield
    reset_rate_limiters()
