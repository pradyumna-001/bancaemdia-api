from __future__ import annotations

import asyncio
import io
import json
import logging
import traceback
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog
from fastapi import FastAPI, Request
from opentelemetry.sdk.trace import TracerProvider
from starlette.middleware.base import BaseHTTPMiddleware

from bancaemdia.observability.logging import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    ResponseStreamAbortedError,
    UnhandledErrorMiddleware,
    UserLogContextMiddleware,
    clear_request_context,
    configure_logging,
    logging_context,
)


@pytest.fixture(autouse=True)
def _restore_logging() -> Any:
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    named = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
            logging.getLogger(name).disabled,
        )
        for name in (
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "celery",
            "celery.task",
            "celery.worker",
            "celery.app.trace",
            "celery.concurrency",
            "celery.concurrency.base",
            "celery.redirected",
            "httpx",
            "multiprocessing",
        )
    }
    structlog_config = structlog.get_config()
    clear_request_context()
    try:
        yield
    finally:
        clear_request_context()
        root.handlers = root_handlers
        root.setLevel(root_level)
        for name, (handlers, propagate, level, disabled) in named.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)
            logger.disabled = disabled
        structlog.configure(**structlog_config)


def _payloads(stream: io.StringIO, message: str | None = None) -> list[dict[str, Any]]:
    payloads = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    if message is None:
        return payloads
    return [payload for payload in payloads if payload.get("message") == message]


def test_structlog_and_stdlib_share_the_required_json_schema() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    with logging_context(request_id="req-1", usuario_id=42):
        structlog.get_logger("application").info("structured", amount=3)
        logging.getLogger("dependency").warning("foreign")

    structured, foreign = _payloads(stream)
    assert structured["amount"] == 3
    assert structured["logger"] == "application"
    assert foreign["logger"] == "dependency"
    assert [structured["message"], foreign["message"]] == ["structured", "foreign"]
    assert [structured["level"], foreign["level"]] == ["info", "warning"]
    for payload in (structured, foreign):
        assert payload["request_id"] == "req-1"
        assert payload["usuario_id"] == 42
        assert payload["trace_id"] is None
        assert payload["span_id"] is None
        assert datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00")).tzinfo


@pytest.mark.parametrize("logger_name", ["celery.task", "multiprocessing"])
def test_worker_text_handlers_are_replaced_by_the_json_pipeline(logger_name: str) -> None:
    stream = io.StringIO()
    legacy = io.StringIO()
    logger = logging.getLogger(logger_name)
    logger.handlers = [logging.StreamHandler(legacy)]
    logger.propagate = False

    configure_logging(stream=stream)
    logger.warning("worker event")

    (payload,) = _payloads(stream)
    assert (payload["logger"], payload["message"]) == (logger_name, "worker event")
    assert legacy.getvalue() == ""


@pytest.mark.parametrize("method", ["GET", "M-SEARCH"])
def test_uvicorn_access_log_redacts_query_strings(method: str) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:1234",
        method,
        "/lookup?token=top-secret&email=user@example.com",
        "1.1",
        200,
    )

    (payload,) = _payloads(stream)
    assert payload["message"] == (f'127.0.0.1:1234 - "{method} /lookup?[REDACTED] HTTP/1.1" 200')
    assert "top-secret" not in stream.getvalue()
    assert "user@example.com" not in stream.getvalue()


def test_httpx_request_log_redacts_query_and_url_credentials() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"',
        "GET",
        "https://person:password@example.test/lookup?token=top-secret&email=user@example.com",
        "HTTP/1.1",
        200,
        "OK",
    )

    (payload,) = _payloads(stream)
    assert payload["message"] == (
        'HTTP Request: GET https://example.test/lookup?[REDACTED] "HTTP/1.1 200 OK"'
    )
    assert "password" not in stream.getvalue()
    assert "top-secret" not in stream.getvalue()
    assert "user@example.com" not in stream.getvalue()


@pytest.mark.parametrize(
    ("level", "message", "expected"),
    [
        (logging.INFO, "Task extraction[id] succeeded in 1s: secret bet", "celery_task_succeeded"),
        (logging.INFO, "Task extraction[id] retry: secret response", "celery_task_retry"),
        (logging.ERROR, "Task extraction[id] failed: secret response", "celery_task_failed"),
    ],
)
def test_celery_task_trace_log_drops_results_and_exception_values(
    level: int, message: str, expected: str
) -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    try:
        raise ValueError("secret traceback")
    except ValueError:
        logging.getLogger("celery.app.trace").log(level, message, exc_info=True)

    (payload,) = _payloads(stream)
    assert payload["message"] == expected
    assert "secret" not in stream.getvalue()
    assert "exception" not in payload


def test_celery_concurrency_debug_logs_cannot_emit_task_arguments() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)

    logging.getLogger("celery.concurrency.base").debug(
        "TaskPool: Apply args=%s", "base64_ULTRASECRET"
    )
    logging.getLogger("celery.concurrency.base").warning("pool warning")

    (payload,) = _payloads(stream)
    assert payload["message"] == "pool warning"
    assert "ULTRASECRET" not in stream.getvalue()


def test_correlation_fields_cannot_be_spoofed_by_a_log_call() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    with logging_context(request_id="real", usuario_id=7):
        structlog.get_logger("test").info(
            "attempt", request_id="fake", usuario_id=999, trace_id="fake", span_id="fake"
        )

    payload = _payloads(stream)[0]
    assert payload["request_id"] == "real"
    assert payload["usuario_id"] == 7
    assert payload["trace_id"] is None
    assert payload["span_id"] is None


def test_active_otel_span_is_copied_to_the_log() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    provider = TracerProvider()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("operation") as span:
        expected = span.get_span_context()
        structlog.get_logger("test").info("inside_span")

    payload = _payloads(stream)[0]
    assert payload["trace_id"] == format(expected.trace_id, "032x")
    assert payload["span_id"] == format(expected.span_id, "016x")
    provider.shutdown()


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        request.state.usuario_id = int(request.headers["X-User-ID"])
        structlog.get_logger("auth").info("authenticated")
        return await call_next(request)


def _application(stream: io.StringIO) -> FastAPI:
    configure_logging(stream=stream)
    app = FastAPI()

    @app.get("/")
    async def endpoint(request: Request) -> dict[str, object]:
        structlog.get_logger("route").info("route", marker=request.headers.get("X-Marker"))
        return {
            "request_id": request.state.request_id,
            "usuario_id": request.state.usuario_id,
        }

    @app.get("/failure")
    async def failure() -> None:
        raise RuntimeError("failure")

    # Starlette executes the last registered middleware first.  Request ID surrounds auth, while
    # the user context runs only after auth has set request.state.usuario_id.
    app.add_middleware(UserLogContextMiddleware)
    app.add_middleware(_FakeAuthMiddleware)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


@pytest.mark.asyncio
async def test_request_id_exists_during_auth_and_user_only_after_auth() -> None:
    stream = io.StringIO()
    app = _application(stream)
    request_id = "e71e6b8a-650e-4b8c-9d67-fcb286015620"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={REQUEST_ID_HEADER: request_id, "X-User-ID": "23"})

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert response.json() == {"request_id": request_id, "usuario_id": 23}
    auth = _payloads(stream, "authenticated")[0]
    route = _payloads(stream, "route")[0]
    assert (auth["request_id"], auth["usuario_id"]) == (request_id, None)
    assert (route["request_id"], route["usuario_id"]) == (request_id, 23)
    assert structlog.contextvars.get_contextvars() == {}


@pytest.mark.asyncio
async def test_invalid_ids_are_replaced_and_concurrent_contexts_do_not_leak() -> None:
    stream = io.StringIO()
    app = _application(stream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first, second = await asyncio.gather(
            client.get(
                "/",
                headers={REQUEST_ID_HEADER: "not-a-uuid", "X-User-ID": "11", "X-Marker": "a"},
            ),
            client.get("/", headers={"X-User-ID": "22", "X-Marker": "b"}),
        )

    first_id = first.headers[REQUEST_ID_HEADER]
    second_id = second.headers[REQUEST_ID_HEADER]
    assert str(UUID(first_id)) == first_id
    assert str(UUID(second_id)) == second_id
    assert first_id != second_id
    routes = {entry["marker"]: entry for entry in _payloads(stream, "route")}
    assert (routes["a"]["request_id"], routes["a"]["usuario_id"]) == (first_id, 11)
    assert (routes["b"]["request_id"], routes["b"]["usuario_id"]) == (second_id, 22)
    assert structlog.contextvars.get_contextvars() == {}


@pytest.mark.asyncio
async def test_unhandled_error_is_correlated_and_context_is_cleared() -> None:
    stream = io.StringIO()
    app = _application(stream)
    request_id = "37086c30-6fa6-4f59-b525-3ca244bf2ee2"
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("server") as span:
        expected_span = span.get_span_context()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/failure", headers={REQUEST_ID_HEADER: request_id, "X-User-ID": "7"}
            )

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert response.json() == {"detail": "Internal server error"}
    (failure,) = _payloads(stream, "unhandled_request_error")
    assert (failure["request_id"], failure["usuario_id"]) == (request_id, 7)
    assert failure["trace_id"] == format(expected_span.trace_id, "032x")
    assert isinstance(failure["span_id"], str) and len(failure["span_id"]) == 16
    assert failure["error_type"] == "RuntimeError"
    assert failure["error_code"] is None
    assert "failure" not in failure.values()
    assert structlog.contextvars.get_contextvars() == {}
    provider.shutdown()


@pytest.mark.asyncio
async def test_streaming_error_is_replaced_before_it_reaches_outer_tracing() -> None:
    secret = "token=ULTRASECRET"
    sent = []

    async def failing_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError(secret)

    async def receive() -> dict[str, object]:
        await asyncio.sleep(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        await asyncio.sleep(0)
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [],
        "state": {},
    }

    with pytest.raises(ResponseStreamAbortedError) as raised:
        await UnhandledErrorMiddleware(failing_app)(scope, receive, send)  # type: ignore[arg-type]

    rendered = "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
    assert str(raised.value) == "response_stream_aborted"
    assert secret not in rendered
    assert sent == [{"type": "http.response.start", "status": 200, "headers": []}]


def test_configuration_is_idempotent_and_rejects_invalid_levels() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    configure_logging(stream=stream)

    structlog.get_logger("test").info("once")

    assert len(_payloads(stream, "once")) == 1
    with pytest.raises(ValueError, match="invalid log level"):
        configure_logging("verbose", stream=stream)
