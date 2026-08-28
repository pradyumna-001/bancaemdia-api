# Week 1 — Database Layer Implementation Plan

**Issue**: #3 — Async Engine, Session, RLS Middleware
**Milestone**: Week 1 — Foundation (PostgreSQL + SQLAlchemy + RLS + CI/CD, Local Docker)
**Size**: L (4-6 hours)

---

## Project Structure Target

```
src/bancaemdia/
├── __init__.py
├── main.py                    # ← update: lifespan, middleware, health, routers
├── core/
│   ├── __init__.py
│   ├── config.py              # ← NEW: Pydantic Settings
│   └── context.py             # ← NEW: ContextVar for usuario_id
├── db/
│   ├── __init__.py
│   ├── session.py             # ← NEW: engine, sessionmaker, get_db(), health check
│   └── models.py              # ← NEW: Base declarative class
├── middleware/
│   ├── __init__.py
│   └── rls.py                 # ← NEW: ASGI middleware for SET LOCAL app.current_user_id
├── api/
│   ├── __init__.py
│   ├── deps.py                # ← NEW: get_current_user, get_db (re-export), service deps
│   └── routes/
│       ├── __init__.py
│       └── health.py          # ← NEW: /health endpoint
└── services/
    └── __init__.py
```

---

## File-by-File Spec

### 1. `src/bancaemdia/core/config.py` (NEW)

Replicates: finAgent/app/core/config.py

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = Field(
        ..., description="Async DSN for SQLAlchemy async engine (postgresql+asyncpg://)"
    )
    MIGRATION_DATABASE_URL: str = Field(
        ..., description="Sync DSN for Alembic migrations (postgresql+psycopg://)"
    )
    SECRET_KEY: str = Field(..., description="FastAPI secret key for JWT signing")


settings = Settings()
```

---

### 2. `src/bancaemdia/core/context.py` (NEW)

Replicates: finAgent/app/core/context.py

```python
from contextvars import ContextVar

current_usuario_id: ContextVar[str | None] = ContextVar("current_usuario_id", default=None)
```

---

### 3. `src/bancaemdia/db/session.py` (NEW)

Replicates: finAgent/app/db/session.py + explicit pool config from issue

```python
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from bancaemdia.core.config import settings
from bancaemdia.core.context import current_usuario_id

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields AsyncSession with RLS already set via ContextVar."""
    async with SessionLocal() as session:
        usuario_id = current_usuario_id.get()
        if usuario_id:
            await session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": usuario_id})
        try:
            yield session
        finally:
            pass  # SET LOCAL is transaction-scoped, auto-resets


async def get_read_db() -> AsyncGenerator[AsyncSession, None]:
    """Optional: separate read-only session if needed later."""
    async with SessionLocal() as session:
        yield session


async def check_db_health() -> bool:
    """Health check: SELECT 1 on primary."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
```

---

### 4. `src/bancaemdia/db/models.py` (NEW)

```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

---

### 5. `src/bancaemdia/middleware/rls.py` (NEW)

Replicates: finAgent/app/middleware/correlation.py pattern — lightweight ContextVar setter

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from bancaemdia.core.context import current_usuario_id


class RLSMiddleware(BaseHTTPMiddleware):
    """Sets current_usuario_id ContextVar from request.state (populated by auth middleware/dependency)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        usuario_id = getattr(request.state, "usuario_id", None)
        token = current_usuario_id.set(usuario_id) if usuario_id else None
        try:
            return await call_next(request)
        finally:
            if token:
                current_usuario_id.reset(token)
```

---

### 6. `src/bancaemdia/api/deps.py` (NEW)

Replicates: finAgent/app/api/deps.py — placeholder for future auth dependency

```python
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.db.session import get_db

security = HTTPBearer(auto_error=False)

# TODO: Implement get_current_user when auth is added
# async def get_current_user(...):
#     ...
```

---

### 7. `src/bancaemdia/main.py` (UPDATE)

Replicates: finAgent/app/main.py

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from bancaemdia.db.session import engine, check_db_health
from bancaemdia.middleware.rls import RLSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: verify DB connectivity
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    yield
    # Shutdown
    await engine.dispose()


app = FastAPI(title="Bancaemdia API", lifespan=lifespan)

# Middleware: LAST added = FIRST to run (closest to request)
app.add_middleware(RLSMiddleware)
# app.add_middleware(AuthMiddleware)  # future


@app.get("/health")
async def health() -> JSONResponse:
    db_ok = await check_db_health()
    if db_ok:
        return JSONResponse({"status": "ok", "db": "ok"})
    return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=503)
```

---

### 8. `alembic/env.py` (VERIFY/UPDATE)

Already uses `async_engine_from_config` — ensure it reads sync URL from alembic.ini (standard practice). No changes needed if alembic.ini has `sqlalchemy.url = postgresql+psycopg://...`.

---

### 9. Tests (NEW)

`tests/integration/test_rls.py` — replicates finAgent/tests/integration/test_rls.py pattern:
- Use testcontainers PostgreSQL
- Create `app.current_user_id` setting via migration
- Test `SET LOCAL` works
- Test cross-tenant isolation returns 0 rows

---

## Dependencies (Already in pyproject.toml ✓)

- `sqlalchemy[asyncio]` ✓
- `asyncpg` ✓
- `pydantic-settings` ✓
- `pytest-asyncio` ✓
- `testcontainers[postgresql]` ✓

---

## Execution Order

| Step | File | Notes |
|------|------|-------|
| 1 | `core/config.py` | Foundation — no deps |
| 2 | `core/context.py` | Simple ContextVar |
| 3 | `db/session.py` | Imports config, creates engine |
| 4 | `db/models.py` | Base class |
| 5 | `middleware/rls.py` | Imports context |
| 6 | `api/deps.py` | Re-exports get_db |
| 7 | `main.py` | Wires everything |
| 8 | `alembic/env.py` | Verify sync URL works |
| 9 | Tests | Verify acceptance criteria |

---

## Acceptance Criteria (from Issue)

- [ ] `async with get_db() as session: await session.execute(text("SELECT current_setting('app.current_user_id')"))` returns test user ID
- [ ] Cross-tenant query returns 0 rows

---

## Key Design Decisions

1. **Engine created at module level** (finAgent pattern) — pool settings explicit per issue
2. **Two URLs**: `DATABASE_URL` (async) for app, `MIGRATION_DATABASE_URL` (sync) for alembic
3. **RLS via ContextVar**: Middleware sets `current_usuario_id` from `request.state`; `get_db()` reads it and runs `SET LOCAL` on session
4. **Middleware order**: `RLSMiddleware` added last → runs first → sets ContextVar before `get_db()` yields
5. **`SET LOCAL`**: Transaction-scoped, auto-resets — no explicit cleanup needed
6. **Health check**: Reuses `check_db_health()` in lifespan and `/health` endpoint

---

## Open Items for Future Issues

- Auth middleware/dependency to populate `request.state.usuario_id`
- User model in `db/models.py`
- Actual RLS policies in PostgreSQL (migration)
- Read replica configuration (currently same as primary for local)