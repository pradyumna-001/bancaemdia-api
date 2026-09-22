import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import Token
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import UUID, uuid4

import structlog
from opentelemetry import trace
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.datastructures import URL, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.contextvars import (
    bind_contextvars,
    bound_contextvars,
    clear_contextvars,
    get_contextvars,
    merge_contextvars,
    reset_contextvars,
)
from structlog.typing import EventDict, Processor, WrappedLogger

REQUEST_ID_HEADER = "X-Request-ID"
_UVICORN_ACCESS_QUERY = re.compile(
    r'(?P<prefix>"[^\s"]+ [^?\s"]+)\?[^"\s]*(?P<suffix> HTTP/\d(?:\.\d+)?")'
)
_HTTPX_REQUEST_URL = re.compile(r'(?P<prefix>\bHTTP Request:\s+[^\s"]+\s+)(?P<url>[^\s"]+)')
_SAFE_ERROR_CODE = re.compile(r"[A-Z0-9]{5}")


def _sanitize_access_log(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    record = event_dict.get("_record")
    if not isinstance(record, logging.LogRecord):
        return event_dict

    event = str(event_dict.get("event", ""))
    if record.name == "uvicorn.access":
        event_dict["event"] = _UVICORN_ACCESS_QUERY.sub(r"\g<prefix>?[REDACTED]\g<suffix>", event)
    elif record.name == "httpx":
        match = _HTTPX_REQUEST_URL.search(event)
        if match is not None:
            try:
                url = URL(match.group("url"))
                safe_url = str(url.replace(username=None, password=None, query="", fragment=""))
                if url.query:
                    safe_url += "?[REDACTED]"
            except (TypeError, ValueError):
                safe_url = "[REDACTED_URL]"
            start, end = match.span("url")
            event_dict["event"] = f"{event[:start]}{safe_url}{event[end:]}"
    elif record.name == "celery.app.trace":
        # Celery formats successful task return values and exception strings into this logger.
        # Both may contain complete extracted bets, uploads or third-party responses.
        if " succeeded in " in event:
            event_dict["event"] = "celery_task_succeeded"
        elif " retry: " in event:
            event_dict["event"] = "celery_task_retry"
        elif record.levelno >= logging.ERROR:
            event_dict["event"] = "celery_task_failed"
        else:
            event_dict["event"] = "celery_task_event"
        event_dict.pop("exc_info", None)
        event_dict.pop("stack_info", None)
    return event_dict


def _correlation_context(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    context = get_contextvars()
    # Correlation fields are authoritative context, not caller-provided log fields.  This prevents
    # an accidental ``logger.info(..., usuario_id=other_user)`` from producing a misleading entry.
    event_dict["request_id"] = context.get("request_id")
    event_dict["usuario_id"] = context.get("usuario_id")

    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    else:
        event_dict["trace_id"] = None
        event_dict["span_id"] = None
    return event_dict


def _timestamp(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    event_dict["timestamp"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return event_dict


def _error_code(error: Exception) -> str | None:
    """Return a bounded SQLSTATE-like code without serializing driver exception text."""

    original = getattr(error, "orig", None)
    code = getattr(original, "sqlstate", None)
    return code if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code) else None


def _shared_processors() -> list[Processor]:
    return [
        merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _sanitize_access_log,
        _correlation_context,
        _timestamp,
    ]


def _json_dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(value, ensure_ascii=False, **kwargs)


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """Configure structlog and stdlib logging as one JSON stream.

    Calling this function again replaces the handler instead of duplicating output.  Libraries
    that use :mod:`logging` and application code that uses :mod:`structlog` therefore share the
    same schema.
    """

    numeric_level = getattr(logging, level.strip().upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"invalid log level: {level!r}")

    processors = _shared_processors()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(serializer=_json_dumps),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Uvicorn and Celery install non-propagating text handlers. Routing them through the root
    # handler keeps API, worker and task logs machine-searchable with the same schema.
    for logger_name in (
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
    ):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.disabled = False
        logger.setLevel(
            logging.WARNING if logger_name.startswith("celery.concurrency") else logging.NOTSET
        )
        logger.propagate = True

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def _request_id(candidate: str | None) -> str:
    if candidate:
        try:
            return str(UUID(candidate.strip()))
        except (ValueError, AttributeError):
            pass
    return str(uuid4())


def bind_request_context(
    *, request_id: str, usuario_id: int | str | None = None
) -> Mapping[str, Token[Any]]:
    """Bind authoritative correlation values and return tokens that must be reset."""

    return bind_contextvars(request_id=request_id, usuario_id=usuario_id)


def reset_request_context(tokens: Mapping[str, Token[Any]]) -> None:
    reset_contextvars(**tokens)


def clear_request_context() -> None:
    """Clear correlation state, primarily at task/request boundaries."""

    clear_contextvars()


@contextmanager
def logging_context(*, request_id: str, usuario_id: int | str | None = None) -> Iterator[None]:
    """Temporarily bind correlation values for workers and command-line jobs."""

    with bound_contextvars(request_id=request_id, usuario_id=usuario_id):
        yield


class RequestIdMiddleware:
    """Create the request correlation context before authentication runs.

    Register this middleware outside the authentication middleware.  A valid incoming UUID is
    preserved for cross-service correlation; arbitrary text is replaced with a new UUID.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        clear_request_context()
        request_id = _request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        tokens = bind_request_context(request_id=request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_context(tokens)
            # The reset restores a possible caller context.  An HTTP task must never retain it.
            clear_request_context()


class UserLogContextMiddleware:
    """Bind the authenticated user for downstream logs.

    Register this middleware inside ``JWTAuthMiddleware`` so ``request.state.usuario_id`` already
    exists.  Keeping this phase separate lets authentication failures retain a request ID without
    falsely claiming a user identity.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        usuario_id = getattr(request.state, "usuario_id", None)
        with bound_contextvars(usuario_id=usuario_id):
            await self.app(scope, receive, send)


class ResponseStreamAbortedError(RuntimeError):
    """A safe replacement for an exception raised after response headers were sent."""


class UnhandledErrorMiddleware:
    """Return a correlated 500 without serializing an exception value into logs or traces."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def track_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, track_response)
        except Exception as error:
            # Downstream user context has already unwound, but auth leaves the verified identity on
            # request.state. Rebind it while the request ID and OTel server span are still active.
            request = Request(scope)
            with bound_contextvars(usuario_id=getattr(request.state, "usuario_id", None)):
                structlog.get_logger(__name__).error(
                    "unhandled_request_error",
                    error_type=type(error).__name__,
                    error_code=_error_code(error),
                )
            if response_started:
                # The connection still needs to abort, but rethrowing the original value lets the
                # outer OTel middleware export exception messages and stack frames containing PII.
                raise ResponseStreamAbortedError("response_stream_aborted") from None
            unavailable = isinstance(error, (OperationalError, InterfaceError))
            response = JSONResponse(
                {"detail": "Service unavailable" if unavailable else "Internal server error"},
                status_code=503 if unavailable else 500,
            )
            await response(scope, receive, send)
