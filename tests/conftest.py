from __future__ import annotations

import os
import sys
from pathlib import Path

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
os.environ.setdefault("JWT_AUDIENCE", "test")
os.environ.setdefault("JWT_ISSUER", "test")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("RATE_LIMIT_STORAGE", "memory://")
os.environ.setdefault("STATEMENT_TIMEOUT_PRIMARY", "5000")
os.environ.setdefault("STATEMENT_TIMEOUT_REPLICA", "30000")
