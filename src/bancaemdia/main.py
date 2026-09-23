import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from bancaemdia.api.contracts import (
    COMMON_ERROR_RESPONSES,
    LivenessResponse,
    ReadinessResponse,
)
from bancaemdia.api.openapi import build_openapi
from bancaemdia.api.v1 import (
    apostas,
    caixa,
    coleta,
    painel,
    revisao,
    telegram,
    titulares,
    upload,
    usuario,
)
from bancaemdia.auth.middleware import JWTAuthMiddleware
from bancaemdia.config import get_settings
from bancaemdia.db.session import LAG_CHECK_SECONDS, engine, replica_engine, replica_lag_seconds
from bancaemdia.middleware.rate_limit import AuthRateLimitMiddleware, RateLimitMiddleware
from bancaemdia.middleware.rls import RLSMiddleware
from bancaemdia.middleware.router import RouterMiddleware
from bancaemdia.observability.alerts import ReplicaLagMonitor
from bancaemdia.observability.health import get_readiness_checker, liveness
from bancaemdia.observability.logging import (
    RequestIdMiddleware,
    UnhandledErrorMiddleware,
    UserLogContextMiddleware,
    configure_logging,
)
from bancaemdia.observability.metrics import instrument_http_metrics, metrics_registry
from bancaemdia.observability.tracing import configure_tracing
from bancaemdia.resilience.circuit_breaker import breaker_states
from bancaemdia.security.http import EndpointBodyLimitMiddleware, SecurityHeadersMiddleware
from bancaemdia.workers.celery_app import get_queue_depth_collector

settings = get_settings()
configure_logging(settings.LOG_LEVEL)


class BancaemdiaAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        return build_openapi(self)


async def _probe_replica_lag() -> float | None:
    async with replica_engine.connect() as conn:
        return await replica_lag_seconds(conn)


replica_lag_monitor = ReplicaLagMonitor(
    _probe_replica_lag,
    interval_seconds=LAG_CHECK_SECONDS,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    replica_lag_task = asyncio.create_task(
        replica_lag_monitor.run(),
        name="replica-lag-monitor",
    )
    try:
        yield
    finally:
        replica_lag_task.cancel()
        with suppress(asyncio.CancelledError):
            await replica_lag_task
        await engine.dispose()
        await replica_engine.dispose()


app = BancaemdiaAPI(
    title="Bancaemdia API",
    version="1.0.0",
    description=(
        "HTTP API for authenticated betting, cash-flow, review, upload, collection, and "
        "dashboard workflows. The checked-in OpenAPI document is the compatibility contract."
    ),
    lifespan=lifespan,
    docs_url=None if settings.APP_ENV == "production" else "/docs",
    redoc_url=None if settings.APP_ENV == "production" else "/redoc",
)
app.include_router(coleta.router)
app.include_router(upload.router)
app.include_router(apostas.router)
app.include_router(caixa.router)
app.include_router(painel.router)
app.include_router(revisao.router)
app.include_router(titulares.router)
app.include_router(telegram.router)
app.include_router(usuario.router)


# O teto de tamanho é registrado primeiro para rodar por DENTRO dos outros: por fora de um
# BaseHTTPMiddleware o 413 dele vira 500 (medido).
app.add_middleware(EndpointBodyLimitMiddleware)
# O roteador precisa do usuário que a autenticação põe no pedido: registrado primeiro, ele roda por
# último, depois da autenticação e do RLS.
app.add_middleware(RouterMiddleware)
app.add_middleware(RLSMiddleware)
# A autenticação já validou e gravou usuario_id quando o limitador roda. O limitador fica antes de
# banco/roteamento e antes da leitura de corpos grandes, recusando abuso com custo baixo.
app.add_middleware(RateLimitMiddleware)
# A autenticação roda antes deste middleware; assim os logs do restante do pedido recebem o usuário,
# enquanto um 401 ainda conserva só o request_id e nunca atribui uma identidade não validada.
app.add_middleware(UserLogContextMiddleware)
# O Starlette roda primeiro o último middleware registrado: a autenticação vem depois do RLS aqui para
# rodar antes dele. Na ordem inversa a rota responde 200 sem enxergar as linhas do usuário (medido).
app.add_middleware(JWTAuthMiddleware)
# Login/refresh traffic must be throttled before authentication, including failed credentials.
# The inner limiter above remains after JWT so every API bucket uses only a validated usuario_id.
app.add_middleware(AuthRateLimitMiddleware)

# Métricas entram depois dos middlewares de domínio, e o request_id por último: entre os middlewares
# da aplicação, o último registrado é o primeiro a rodar e envolve autenticação e métricas. O OTel
# instala por fora sua própria camada ao construir a pilha, deixando o span ativo para todos eles.
instrument_http_metrics(app)
app.add_middleware(UnhandledErrorMiddleware)
app.add_middleware(RequestIdMiddleware)
configure_tracing(
    app=app,
    engines=(engine, replica_engine),
    environment=settings.APP_ENV,
    otlp_endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
)
app.add_middleware(SecurityHeadersMiddleware)


@app.get(
    "/health",
    response_model=LivenessResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def health() -> JSONResponse:
    return JSONResponse(liveness())


@app.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={
        **COMMON_ERROR_RESPONSES,
        503: {
            "model": ReadinessResponse,
            "description": "One or more required dependencies are not ready.",
        },
    },
)
async def ready() -> JSONResponse:
    report = await get_readiness_checker().check()
    return JSONResponse(report.as_dict(), status_code=report.status_code)


@app.get(
    "/metrics",
    response_class=Response,
    responses={
        **COMMON_ERROR_RESPONSES,
        200: {
            "description": "Prometheus exposition document.",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
    },
)
async def metrics() -> Response:
    # Quem troca o estado do disjuntor são os trabalhadores; lendo o Redis aqui, a API exporta o
    # estado compartilhado e não o último que este processo viu.
    await run_in_threadpool(breaker_states)
    registry = metrics_registry(get_queue_depth_collector())
    body = await run_in_threadpool(generate_latest, registry)
    return Response(body, media_type=CONTENT_TYPE_LATEST)
