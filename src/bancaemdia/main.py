import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from bancaemdia.api.contracts import (
    COMMON_ERROR_RESPONSES,
    LivenessResponse,
    ReadinessResponse,
)
from bancaemdia.api.identity import failure
from bancaemdia.api.identity import router as identity_router
from bancaemdia.api.openapi import build_openapi
from bancaemdia.api.v1 import (
    admin_casas,
    aposta_foto,
    apostas,
    apostas_lotes,
    bancas,
    billing,
    billing_webhook,
    caixa,
    calculators,
    coleta,
    coleta_catalogo,
    coleta_pairing,
    coleta_sessoes,
    filtros,
    hipotetica,
    manual_bets,
    metas,
    painel,
    painel_detalhado,
    painel_filtrado,
    results,
    reviews,
    revisao,
    telegram,
    titulares,
    unidade,
    upload,
    usuario,
    web_print,
)
from bancaemdia.auth.identity_config import identity_settings
from bancaemdia.auth.identity_service import identity_service
from bancaemdia.auth.middleware import JWTAuthMiddleware, route_path
from bancaemdia.auth.oidc import IdentityError
from bancaemdia.auth.transport import IdentityTransportMiddleware
from bancaemdia.config import get_settings
from bancaemdia.db.session import (
    LAG_CHECK_SECONDS,
    engine,
    prewarm_pool,
    replica_engine,
    replica_lag_seconds,
)
from bancaemdia.domain.access import AccountReadOnlyError
from bancaemdia.integrations.telegram import webhook as telegram_webhook
from bancaemdia.middleware.coleta_body import CollectionBodyLimit
from bancaemdia.middleware.coleta_credentials import CollectionCredentialMiddleware
from bancaemdia.middleware.compression import ReadCompressionMiddleware
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
    replica_lag_task: asyncio.Task[None] | None = None
    if identity_settings().AUTH_ENABLED:
        await identity_service().verify_database_role()
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        if settings.DB_POOL_PREWARM:
            await prewarm_pool(engine, settings.DB_POOL_SIZE)
            await prewarm_pool(replica_engine, settings.DB_POOL_SIZE)
        replica_lag_task = asyncio.create_task(
            replica_lag_monitor.run(),
            name="replica-lag-monitor",
        )
        yield
    finally:
        if replica_lag_task is not None:
            replica_lag_task.cancel()
            with suppress(asyncio.CancelledError):
                await replica_lag_task
        await engine.dispose()
        await replica_engine.dispose()
        if identity_settings().AUTH_ENABLED:
            await identity_service().engine.dispose()


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
app.include_router(coleta_pairing.router)
app.include_router(coleta_sessoes.router)
app.include_router(coleta_catalogo.router)
app.include_router(admin_casas.router)
app.include_router(upload.router)
app.include_router(web_print.router)
app.include_router(identity_router)
app.include_router(apostas.router)
app.include_router(manual_bets.router)
app.include_router(reviews.router)
app.include_router(results.router)
app.include_router(aposta_foto.router)
app.include_router(caixa.router)
app.include_router(painel.router)
app.include_router(metas.router)
app.include_router(painel_filtrado.router)
app.include_router(painel_detalhado.router)
app.include_router(filtros.router)
app.include_router(hipotetica.router)
app.include_router(revisao.router)
app.include_router(calculators.router)
app.include_router(billing_webhook.router)
app.include_router(billing.router)
app.include_router(titulares.router)
app.include_router(apostas_lotes.router)
app.include_router(bancas.router)
app.include_router(unidade.router)
app.include_router(telegram.router)
app.include_router(telegram_webhook.router)
app.include_router(usuario.router)


@app.exception_handler(RequestValidationError)
async def validation_failure(request: Request, error: RequestValidationError) -> JSONResponse:
    if route_path(request).startswith(apostas_lotes.PREFIXO):
        return apostas_lotes.validacao(error)
    if route_path(request).startswith(bancas.PREFIXO):
        return bancas.validacao(error)
    if route_path(request).startswith(unidade.PREFIXO):
        return unidade.validacao(error)
    if route_path(request).startswith("/api/v1/titulares/trocas"):
        return JSONResponse(status_code=422, content={"detail": "swap_invalid_request"})
    if route_path(request).startswith("/api/v1/apostas-manuais/"):
        return manual_bets.validation_failure(error)
    if request.url.path.startswith("/api/v1/revisao-operacional/"):
        errors = error.errors()
        from bancaemdia.domain.aposta_service import CAMPOS_CORRIGIVEIS
        from bancaemdia.domain.review_contract import ReviewDecision, ReviewQuery

        allowed_fields = (
            CAMPOS_CORRIGIVEIS
            | set(ReviewDecision.model_fields)
            | set(ReviewQuery.model_fields)
            | {"operacao_id"}
        )
        location = errors[0]["loc"] if errors else ()
        name = str(location[-1]) if location else None
        field = (
            name
            if name in allowed_fields
            else "aposta_corrigida"
            if "aposta_corrigida" in location
            else None
        )
        return JSONResponse(
            {"detail": "review_validation_invalid", "campo": field}, status_code=422
        )
    from bancaemdia.domain.foto_aposta import photo_path

    if route_path(request).startswith("/api/v1/prints/"):
        return aposta_foto.photo_error(422, "o pedido de prints é inválido")
    if photo_path(route_path(request)):
        return aposta_foto.photo_error(422, "a chave da aposta é inválida")
    if route_path(request).startswith("/auth/"):
        return failure(IdentityError("invalid_request", 422))
    return await request_validation_exception_handler(request, error)


# O teto de tamanho é registrado primeiro para rodar por DENTRO dos outros: por fora de um
# BaseHTTPMiddleware o 413 dele vira 500 (medido).
app.add_middleware(CollectionBodyLimit)
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
app.add_middleware(IdentityTransportMiddleware)
app.add_middleware(CollectionCredentialMiddleware)

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
# A compressão envolve request_id, erros e métricas: todos eles continuam vendo o JSON original.
app.add_middleware(ReadCompressionMiddleware)
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


@app.exception_handler(AccountReadOnlyError)
def account_read_only_error(request: Request, exc: AccountReadOnlyError) -> JSONResponse:
    return JSONResponse(status_code=402, content={"detail": "account_read_only"})


@app.exception_handler(DBAPIError)
def billing_database_error(request: Request, exc: DBAPIError) -> JSONResponse:
    from bancaemdia.domain.foto_aposta import photo_path

    if photo_path(route_path(request)):
        return aposta_foto.photo_error(503, aposta_foto.UNAVAILABLE)
    if getattr(exc.orig, "sqlstate", None) == "P0402":
        return JSONResponse(status_code=402, content={"detail": "account_read_only"})
    if route_path(request).startswith("/api/v1/prints/"):
        return aposta_foto.photo_error(503, "não foi possível consultar os prints agora")
    raise exc
