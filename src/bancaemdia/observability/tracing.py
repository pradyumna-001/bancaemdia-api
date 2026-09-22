from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Literal, cast

import structlog
from celery import signals as celery_signals
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor, RequestInfo
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
from opentelemetry.instrumentation.sqlalchemy.engine import (
    _handle_error as _otel_sql_error_handler,
)
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode, Tracer, TracerProvider
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import URL
from starlette.types import Scope

type CustomSpanName = Literal[
    "upload.parse",
    "extraction.chamar_anthropic",
    "materializacao.upsert",
    "cruzamento.pareador",
    "coleta.casa",
]
type SpanAttribute = str | bool | int | float | None

MAX_ATTRIBUTE_LENGTH = 256
REDACTED = "[REDACTED]"

_ALLOWED_ATTRIBUTES: Mapping[CustomSpanName, frozenset[str]] = {
    "upload.parse": frozenset({"file_size", "message_count"}),
    "extraction.chamar_anthropic": frozenset({"model", "versao_prompt", "chat_id", "message_id"}),
    "materializacao.upsert": frozenset({"origem", "usuario_id"}),
    "cruzamento.pareador": frozenset({"aposta_nova_id", "aposta_existente_id", "resultado"}),
    "coleta.casa": frozenset({"casa", "quantidade", "usuario_id"}),
}

_setup_lock = Lock()
_owned_provider: SDKTracerProvider | None = None
_exporter_endpoint: str | None = None
_httpx_hooks_installed = False
_CELERY_FAILURE_HANDLER_UID = "bancaemdia.otel.safe_task_failure"
_CELERY_RETRY_HANDLER_UID = "bancaemdia.otel.safe_task_retry"
_SQL_OPERATION = re.compile(r"^\s*(?:/\*.*?\*/\s*)*([A-Za-z]+)", re.DOTALL)
_SAFE_SQL_OPERATIONS = frozenset({
    "ALTER",
    "BEGIN",
    "CALL",
    "COMMIT",
    "CREATE",
    "DELETE",
    "DROP",
    "EXPLAIN",
    "INSERT",
    "MERGE",
    "PRAGMA",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
    "SELECT",
    "SET",
    "TRUNCATE",
    "UPDATE",
    "WITH",
})


@dataclass(frozen=True)
class TracingSetup:
    provider: TracerProvider
    exporter_enabled: bool
    instrumented: frozenset[str]


def _create_otlp_exporter(endpoint: str) -> SpanExporter | None:
    try:
        # The exporter is deliberately lazy: local/test installs may use tracing without shipping
        # spans, and a missing optional exporter must not stop the API from starting.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint)
    except Exception as error:
        structlog.get_logger(__name__).warning(
            "otel_exporter_unavailable", error=type(error).__name__
        )
        return None


def _provider(
    *, service_name: str, environment: str | None, otlp_endpoint: str | None
) -> tuple[TracerProvider, bool]:
    global _exporter_endpoint, _owned_provider

    current = trace.get_tracer_provider()
    if isinstance(current, trace.ProxyTracerProvider):
        attributes: dict[str, str] = {SERVICE_NAME: service_name}
        if environment:
            attributes[DEPLOYMENT_ENVIRONMENT] = environment
        candidate = SDKTracerProvider(resource=Resource.create(attributes))
        trace.set_tracer_provider(candidate)
        current = trace.get_tracer_provider()
        if current is candidate:
            _owned_provider = candidate
        else:
            candidate.shutdown()

    exporter_enabled = False
    if otlp_endpoint and current is _owned_provider:
        if _exporter_endpoint == otlp_endpoint:
            exporter_enabled = True
        elif _exporter_endpoint is None:
            exporter = _create_otlp_exporter(otlp_endpoint)
            if exporter is not None:
                _owned_provider.add_span_processor(BatchSpanProcessor(exporter))
                _exporter_endpoint = otlp_endpoint
                exporter_enabled = True
        else:
            structlog.get_logger(__name__).warning("otel_exporter_already_configured")
    elif otlp_endpoint and current is not _owned_provider:
        # An agent/distro configured OTel before the app.  Reuse it and avoid a second exporter,
        # which would duplicate every span and may send it to an unexpected backend.
        structlog.get_logger(__name__).info("otel_provider_managed_externally")

    return current, exporter_enabled


def _safe_http_url(request: RequestInfo) -> str:
    return str(request.url.copy_with(query=None, fragment=None, userinfo=None))


def _sanitize_httpx_request(span: Span, request: RequestInfo) -> None:
    if not span.is_recording():
        return
    safe_url = _safe_http_url(request)
    # Cover both stable and legacy semantic conventions.  Query parameters and userinfo can carry
    # tokens or PII, and are not needed to identify the downstream dependency.
    span.set_attribute("url.full", safe_url)
    span.set_attribute("http.url", safe_url)
    if request.url.query:
        span.set_attribute("url.query", REDACTED)
    span.set_attribute("http.target", request.url.path)


async def _sanitize_async_httpx_request(  # ruff: ignore[unused-async] - API requires a coroutine
    span: Span, request: RequestInfo
) -> None:
    _sanitize_httpx_request(span, request)


def _sanitize_server_request(span: Span, scope: Scope) -> None:
    if not span.is_recording() or not scope.get("query_string"):
        return
    safe_url = str(URL(scope=scope).replace(query=""))
    span.set_attribute("http.url", safe_url)
    span.set_attribute("url.full", safe_url)
    span.set_attribute("url.query", REDACTED)
    span.set_attribute("http.target", str(scope.get("path", "")))


def _instrument_app(app: FastAPI, provider: TracerProvider) -> None:
    marker = "_bancaemdia_tracing_installed"
    if getattr(app.state, marker, False):
        return
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        # `opentelemetry-instrument` may have instrumented the app constructor before import. Its
        # default middleware records raw query strings and send/receive spans, so replace that
        # instance with the same provider plus the privacy hooks below.
        FastAPIInstrumentor.uninstrument_app(app)
        app.middleware_stack = None
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=_sanitize_server_request,
        # Do not capture request/response headers.  Authorization, cookies and application
        # payloads must stay out of telemetry.
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
        exclude_spans=["receive", "send"],
    )
    setattr(app.state, marker, True)


def _safe_celery_failure(*args: object, **kwargs: object) -> None:
    """Mark a task span failed without exporting exception messages or stack frames."""

    span = trace.get_current_span()
    if span.is_recording():
        span.set_status(Status(StatusCode.ERROR))


def _safe_celery_retry(*args: object, **kwargs: object) -> None:
    """Record that an attempt will retry without exporting Celery's reason string."""

    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("celery.retry", True)


def _replace_sensitive_celery_handlers(instrumentor: CeleryInstrumentor) -> None:
    # The upstream handlers serialize ``str(exception)``, stack traces and retry reasons into the
    # span. Extraction failures can contain full third-party payloads, so retain only state.
    celery_signals.task_failure.disconnect(instrumentor._trace_failure)
    celery_signals.task_retry.disconnect(instrumentor._trace_retry)
    celery_signals.task_failure.connect(
        _safe_celery_failure, weak=False, dispatch_uid=_CELERY_FAILURE_HANDLER_UID
    )
    celery_signals.task_retry.connect(
        _safe_celery_retry, weak=False, dispatch_uid=_CELERY_RETRY_HANDLER_UID
    )


def _safe_sql_operation(statement: object) -> str:
    if not isinstance(statement, str):
        return "SQL"
    match = _SQL_OPERATION.match(statement)
    operation = match.group(1).upper() if match is not None else "SQL"
    return operation if operation in _SAFE_SQL_OPERATIONS else "SQL"


def _sanitize_sql_span(
    _connection: object,
    _cursor: object,
    statement: object,
    _parameters: object,
    context: object,
    _executemany: object,
) -> None:
    """Replace query text after the OTel listener creates its span, before execution."""

    span = getattr(context, "_otel_span", None)
    is_recording = getattr(span, "is_recording", None)
    if not callable(is_recording) or not is_recording():
        return
    recording_span = cast(Span, span)
    operation = _safe_sql_operation(statement)
    recording_span.update_name(operation)
    # Cover both legacy and stable database semantic conventions.
    recording_span.set_attribute("db.statement", operation)
    recording_span.set_attribute("db.query.text", operation)


def _sanitize_sql_error(exception_context: object) -> None:
    """End a failed SQL span with status only, never the driver exception value."""

    execution_context = getattr(exception_context, "execution_context", None)
    span = getattr(execution_context, "_otel_span", None)
    is_recording = getattr(span, "is_recording", None)
    if callable(is_recording) and is_recording():
        recording_span = cast(Span, span)
        recording_span.set_status(Status(StatusCode.ERROR))
        recording_span.end()


def _replace_sql_error_handler(engine: Engine) -> None:
    if sqlalchemy_event.contains(engine, "handle_error", _sanitize_sql_error):
        return
    if sqlalchemy_event.contains(engine, "handle_error", _otel_sql_error_handler):
        sqlalchemy_event.remove(engine, "handle_error", _otel_sql_error_handler)
        for registration in tuple(EngineTracer._remove_event_listener_params):
            target, identifier, function = registration
            if (
                target() is engine
                and identifier == "handle_error"
                and function is _otel_sql_error_handler
            ):
                EngineTracer._dispose_of_event_listener(registration)  # type: ignore[no-untyped-call]
    # Use the instrumentor's own bookkeeping so a later `uninstrument()` removes our replacement.
    EngineTracer._register_event_listener(  # type: ignore[no-untyped-call]
        engine, "handle_error", _sanitize_sql_error
    )


def _install_sql_span_sanitizers(engines: Iterable[Engine]) -> None:
    for engine in engines:
        if not sqlalchemy_event.contains(engine, "before_cursor_execute", _sanitize_sql_span):
            sqlalchemy_event.listen(engine, "before_cursor_execute", _sanitize_sql_span)
        _replace_sql_error_handler(engine)


def _instrument_dependencies(
    *, provider: TracerProvider, engines: Iterable[AsyncEngine | Engine]
) -> frozenset[str]:
    global _httpx_hooks_installed

    active: set[str] = set()

    httpx = HTTPXClientInstrumentor()
    if not _httpx_hooks_installed:
        if httpx.is_instrumented_by_opentelemetry:
            # Replace CLI/default instrumentation so URL credentials and queries cannot be
            # exported before our request hooks run.
            httpx.uninstrument()
        httpx.instrument(
            tracer_provider=provider,
            request_hook=_sanitize_httpx_request,
            async_request_hook=_sanitize_async_httpx_request,
        )
        _httpx_hooks_installed = True
    active.add("httpx")

    redis = RedisInstrumentor()
    if not redis.is_instrumented_by_opentelemetry:
        # Current Redis instrumentation records command names and replaces every argument with
        # ``?``; no Redis values are captured.
        redis.instrument(tracer_provider=provider)
    active.add("redis")

    sync_engines: list[Engine] = []
    seen: set[int] = set()
    for engine in engines:
        sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
        if id(sync_engine) not in seen:
            seen.add(id(sync_engine))
            sync_engines.append(sync_engine)

    sqlalchemy = SQLAlchemyInstrumentor()
    if not sqlalchemy.is_instrumented_by_opentelemetry:
        kwargs: dict[str, object] = {
            "tracer_provider": provider,
            "enable_commenter": False,
            "enable_attribute_commenter": False,
        }
        if sync_engines:
            kwargs["engines"] = sync_engines
        sqlalchemy.instrument(**kwargs)
    _install_sql_span_sanitizers(sync_engines)
    active.add("sqlalchemy")

    celery = CeleryInstrumentor()  # type: ignore[no-untyped-call]
    if not celery.is_instrumented_by_opentelemetry:
        celery.instrument(tracer_provider=provider)
    _replace_sensitive_celery_handlers(celery)
    active.add("celery")
    return frozenset(active)


def configure_tracing(
    *,
    app: FastAPI | None = None,
    engines: Iterable[AsyncEngine | Engine] = (),
    service_name: str = "bancaemdia-api",
    environment: str | None = None,
    otlp_endpoint: str | None = None,
) -> TracingSetup:
    """Configure OTel once and instrument the app and common dependencies.

    An absent or unusable OTLP exporter degrades to local, non-exported spans.  If an external OTel
    agent configured the global provider first, it remains authoritative.
    """

    endpoint = otlp_endpoint.strip() if otlp_endpoint and otlp_endpoint.strip() else None
    with _setup_lock:
        provider, exporter_enabled = _provider(
            service_name=service_name,
            environment=environment,
            otlp_endpoint=endpoint,
        )
        instrumented = set(_instrument_dependencies(provider=provider, engines=engines))
        if app is not None:
            _instrument_app(app, provider)
            instrumented.add("fastapi")
    return TracingSetup(
        provider=provider,
        exporter_enabled=exporter_enabled,
        instrumented=frozenset(instrumented),
    )


def shutdown_owned_tracing(timeout_millis: int = 5_000) -> None:
    """Flush the current provider and close it only when this process owns it."""

    owned_provider = _owned_provider
    provider = owned_provider or trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if not callable(force_flush):
        return
    try:
        force_flush(timeout_millis=timeout_millis)
    except Exception as error:
        structlog.get_logger(__name__).warning(
            "otel_force_flush_failed", error=type(error).__name__
        )
    finally:
        if owned_provider is not None:
            owned_provider.shutdown()


def _tracer() -> Tracer:
    return trace.get_tracer("bancaemdia")


def _safe_attributes(
    name: CustomSpanName, attributes: Mapping[str, SpanAttribute]
) -> dict[str, str | bool | int | float]:
    unexpected = set(attributes).difference(_ALLOWED_ATTRIBUTES[name])
    if unexpected:
        fields = ", ".join(sorted(unexpected))
        raise ValueError(f"attributes not allowed for {name}: {fields}")

    safe: dict[str, str | bool | int | float] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        safe[key] = value[:MAX_ATTRIBUTE_LENGTH] if isinstance(value, str) else value
    return safe


def set_custom_span_attributes(
    span: Span, name: CustomSpanName, **attributes: SpanAttribute
) -> None:
    """Attach only the issue-approved, bounded attributes to an existing span."""

    for key, value in _safe_attributes(name, attributes).items():
        span.set_attribute(key, value)


@contextmanager
def custom_span(name: CustomSpanName, **attributes: SpanAttribute) -> Iterator[Span]:
    """Start one of the approved business spans without accepting arbitrary PII fields."""

    with _tracer().start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        set_custom_span_attributes(span, name, **attributes)
        try:
            yield span
        except Exception:
            # Exception messages and stack frames can contain uploaded text, model responses and
            # database values. Preserve failure visibility without exporting those values.
            span.set_status(Status(StatusCode.ERROR))
            raise
