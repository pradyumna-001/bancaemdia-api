from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import text

from bancaemdia.db.session import check_db_health, engine
from bancaemdia.middleware.rls import RLSMiddleware
from bancaemdia.resilience.circuit_breaker import breaker_states


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="Bancaemdia API")


app.add_middleware(RLSMiddleware)


@app.get("/health")
async def health() -> JSONResponse:
    db_ok = await check_db_health()
    circuit_breakers = await run_in_threadpool(breaker_states)
    if db_ok:
        return JSONResponse({"status": "ok", "db": "ok", "circuit_breakers": circuit_breakers})
    return JSONResponse(
        {"status": "degraded", "db": "unreachable", "circuit_breakers": circuit_breakers},
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
