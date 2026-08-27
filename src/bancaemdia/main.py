from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from bancaemdia.db.session import check_db_health, engine
from bancaemdia.middleware.rls import RLSMiddleware


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
    if db_ok:
        return JSONResponse({"status": "ok", "db": "ok"})
    return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
