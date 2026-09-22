from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from bancaemdia.api.v1 import apostas, caixa, coleta, painel, revisao, upload
from bancaemdia.auth.middleware import JWTAuthMiddleware
from bancaemdia.config import get_settings
from bancaemdia.db.session import engine, replica_engine
from bancaemdia.middleware.rls import RLSMiddleware
from bancaemdia.middleware.router import RouterMiddleware
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
from bancaemdia.workers.celery_app import get_queue_depth_collector

settings = get_settings()
configure_logging(settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await engine.dispose()
        await replica_engine.dispose()


app = FastAPI(title="Bancaemdia API")
app.state.limiter = coleta.limiter
app.add_exception_handler(RateLimitExceeded, coleta.limite_estourado)
app.include_router(coleta.router)
app.include_router(upload.router)
app.include_router(apostas.router)
app.include_router(caixa.router)
app.include_router(painel.router)
app.include_router(revisao.router)


# O teto de tamanho é registrado primeiro para rodar por DENTRO dos outros: por fora de um
# BaseHTTPMiddleware o 413 dele vira 500 (medido).
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_size=get_settings().UPLOAD_MAX_BYTES + upload.MARGEM_DO_FORMULARIO,
)
# O roteador precisa do usuário que a autenticação põe no pedido: registrado primeiro, ele roda por
# último, depois da autenticação e do RLS.
app.add_middleware(RouterMiddleware)
app.add_middleware(RLSMiddleware)
# A autenticação roda antes deste middleware; assim os logs do restante do pedido recebem o usuário,
# enquanto um 401 ainda conserva só o request_id e nunca atribui uma identidade não validada.
app.add_middleware(UserLogContextMiddleware)
# O Starlette roda primeiro o último middleware registrado: a autenticação vem depois do RLS aqui para
# rodar antes dele. Na ordem inversa a rota responde 200 sem enxergar as linhas do usuário (medido).
app.add_middleware(JWTAuthMiddleware)

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


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(liveness())


@app.get("/ready")
async def ready() -> JSONResponse:
    report = await get_readiness_checker().check()
    return JSONResponse(report.as_dict(), status_code=report.status_code)


@app.get("/metrics")
async def metrics() -> Response:
    # Quem troca o estado do disjuntor são os trabalhadores; lendo o Redis aqui, a API exporta o
    # estado compartilhado e não o último que este processo viu.
    await run_in_threadpool(breaker_states)
    registry = metrics_registry(get_queue_depth_collector())
    body = await run_in_threadpool(generate_latest, registry)
    return Response(body, media_type=CONTENT_TYPE_LATEST)
