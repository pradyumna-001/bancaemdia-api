from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from bancaemdia.db.session import check_db_health, engine
from bancaemdia.middleware.rls import RLSMiddleware
from bancaemdia.observability.metrics import metrics_registry
from bancaemdia.resilience.circuit_breaker import breaker_states
from bancaemdia.workers.celery_app import get_queue_depth_collector


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


@app.get("/metrics")
async def metrics() -> Response:
    # Quem troca o estado do disjuntor são os trabalhadores; lendo o Redis aqui, a API exporta o
    # estado compartilhado e não o último que este processo viu.
    await run_in_threadpool(breaker_states)
    registry = metrics_registry(get_queue_depth_collector())
    body = await run_in_threadpool(generate_latest, registry)
    return Response(body, media_type=CONTENT_TYPE_LATEST)
