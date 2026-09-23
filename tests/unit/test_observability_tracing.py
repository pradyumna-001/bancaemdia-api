from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracerProvider, StatusCode
from sqlalchemy import create_engine
from structlog.testing import capture_logs

from bancaemdia.observability import tracing


def test_custom_spans_record_only_the_approved_bounded_attributes(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", lambda: provider.get_tracer("test"))

    with tracing.custom_span(
        "extraction.chamar_anthropic",
        model="m" * 300,
        versao_prompt="v2",
        chat_id=123,
        message_id=None,
    ):
        pass

    span = exporter.get_finished_spans()[0]
    assert span.name == "extraction.chamar_anthropic"
    assert span.attributes == {
        "model": "m" * tracing.MAX_ATTRIBUTE_LENGTH,
        "versao_prompt": "v2",
        "chat_id": 123,
    }
    provider.shutdown()


def test_custom_span_failure_does_not_export_exception_values(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", lambda: provider.get_tracer("test"))
    secret = "email=secret@example.test"

    with pytest.raises(ValueError, match="secret"):
        with tracing.custom_span("upload.parse", file_size=10):
            raise ValueError(secret)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert span.events == ()
    assert secret not in repr(span.attributes)
    provider.shutdown()


def test_celery_failure_and_retry_handlers_do_not_export_reason_values() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    secret = "email=secret@example.test"

    with tracer.start_as_current_span("celery.run"):
        tracing._safe_celery_retry(reason=ValueError(secret))
        tracing._safe_celery_failure(einfo=ValueError(secret))

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert span.attributes == {"celery.retry": True}
    assert span.events == ()
    assert secret not in repr(span)
    provider.shutdown()


def test_sql_span_sanitizers_remove_statement_and_driver_error_values() -> None:
    class RecordingSpan:
        def __init__(self) -> None:
            self.name = "SELECT database"
            self.attributes = {"db.statement": "SELECT token_ULTRASECRET"}
            self.status = None

        def is_recording(self) -> bool:
            return True

        def update_name(self, name: str) -> None:
            self.name = name

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

        def set_status(self, status: object) -> None:
            self.status = status

        def end(self) -> None:
            pass

    span = RecordingSpan()
    execution_context = SimpleNamespace(_otel_span=span)

    tracing._sanitize_sql_span(  # type: ignore[arg-type]
        object(),
        object(),
        "SELECT token_ULTRASECRET FROM private_table",
        {},
        execution_context,
        False,
    )
    tracing._sanitize_sql_error(  # type: ignore[arg-type]
        SimpleNamespace(execution_context=execution_context)
    )

    assert span.name == "SELECT"
    assert span.attributes == {"db.statement": "SELECT", "db.query.text": "SELECT"}
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert "ULTRASECRET" not in repr(span.__dict__)


def test_real_sqlalchemy_instrumentation_exports_only_sanitized_sql() -> None:
    script = "\n".join([
        "from opentelemetry.sdk.trace import TracerProvider",
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor",
        "from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter",
        "from sqlalchemy import create_engine, text",
        "from bancaemdia.observability import tracing",
        "exporter = InMemorySpanExporter()",
        "provider = TracerProvider()",
        "provider.add_span_processor(SimpleSpanProcessor(exporter))",
        "engine = create_engine('sqlite://')",
        "tracing._instrument_dependencies(provider=provider, engines=(engine,))",
        "try:",
        "    with engine.connect() as connection:",
        "        connection.execute(text('SELECT token_ULTRASECRET'))",
        "except Exception:",
        "    pass",
        "payload = '\\n'.join(span.to_json() for span in exporter.get_finished_spans())",
        "assert exporter.get_finished_spans()",
        "assert 'ULTRASECRET' not in payload, payload",
        "assert '\"description\"' not in payload, payload",
        "print('sanitized')",
        "provider.shutdown()",
    ])

    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "sanitized"


@pytest.mark.parametrize("field", ["email", "password", "token", "payload", "message_body"])
def test_custom_spans_reject_unapproved_pii_fields(monkeypatch, field: str) -> None:
    provider = TracerProvider()
    monkeypatch.setattr(tracing, "_tracer", lambda: provider.get_tracer("test"))

    with pytest.raises(ValueError, match=field):
        with tracing.custom_span("upload.parse", **{field: "secret"}):
            pass

    provider.shutdown()


def test_httpx_hook_removes_query_fragment_and_credentials() -> None:
    class RecordingSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    request = tracing.RequestInfo(
        method=b"GET",
        url=httpx.URL("https://person:secret@example.test/resource?token=secret#fragment"),
        headers=None,
        stream=None,
        extensions=None,
    )
    span = RecordingSpan()

    tracing._sanitize_httpx_request(span, request)  # type: ignore[arg-type]

    assert span.attributes["url.full"] == "https://example.test/resource"
    assert span.attributes["http.url"] == "https://example.test/resource"
    assert span.attributes["url.query"] == tracing.REDACTED
    assert "secret" not in repr(span.attributes)


def test_server_hook_redacts_query_values() -> None:
    class RecordingSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    scope = {
        "type": "http",
        "scheme": "https",
        "server": ("api.example.test", 443),
        "path": "/lookup",
        "root_path": "",
        "query_string": b"token=secret",
        "headers": [],
    }
    span = RecordingSpan()

    tracing._sanitize_server_request(span, scope)  # type: ignore[arg-type]

    assert span.attributes["http.url"] == "https://api.example.test/lookup"
    assert span.attributes["url.query"] == tracing.REDACTED
    assert "secret" not in repr(span.attributes)


def test_missing_otlp_exporter_does_not_break_startup(monkeypatch) -> None:
    module = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    monkeypatch.setitem(sys.modules, module, None)

    with capture_logs() as logs:
        exporter = tracing._create_otlp_exporter("http://collector.test:4317")

    assert exporter is None
    assert logs == [
        {
            "error": "ModuleNotFoundError",
            "event": "otel_exporter_unavailable",
            "log_level": "warning",
        }
    ]


class _FakeInstrumentor:
    calls: ClassVar[list[dict[str, object]]]
    active: ClassVar[bool]
    uninstrument_calls: ClassVar[int]

    def __init_subclass__(cls) -> None:
        cls.calls = []
        cls.active = False
        cls.uninstrument_calls = 0

    @property
    def is_instrumented_by_opentelemetry(self) -> bool:
        return type(self).active

    def instrument(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)
        type(self).active = True

    def uninstrument(self) -> None:
        type(self).uninstrument_calls += 1
        type(self).active = False


class _FakeHTTPX(_FakeInstrumentor):
    pass


class _FakeRedis(_FakeInstrumentor):
    pass


class _FakeSQLAlchemy(_FakeInstrumentor):
    pass


class _FakeCelery(_FakeInstrumentor):
    @staticmethod
    def _trace_failure(*args: object, **kwargs: object) -> None:
        pass

    @staticmethod
    def _trace_retry(*args: object, **kwargs: object) -> None:
        pass


class _FakeFastAPI:
    calls: ClassVar[list[dict[str, object]]] = []
    uninstrumented: ClassVar[list[FastAPI]] = []

    @staticmethod
    def instrument_app(app: FastAPI, **kwargs: object) -> None:
        _FakeFastAPI.calls.append(kwargs)
        app._is_instrumented_by_opentelemetry = True

    @staticmethod
    def uninstrument_app(app: FastAPI) -> None:
        _FakeFastAPI.uninstrumented.append(app)
        app._is_instrumented_by_opentelemetry = False


def test_configuration_instruments_each_dependency_only_once(monkeypatch) -> None:
    for fake in (_FakeHTTPX, _FakeRedis, _FakeSQLAlchemy, _FakeCelery):
        fake.calls = []
        fake.active = False
        fake.uninstrument_calls = 0
    _FakeFastAPI.calls = []
    _FakeFastAPI.uninstrumented = []
    monkeypatch.setattr(tracing, "_httpx_hooks_installed", False)
    provider = NoOpTracerProvider()
    monkeypatch.setattr(tracing, "_provider", lambda **_: (provider, False))
    monkeypatch.setattr(tracing, "HTTPXClientInstrumentor", _FakeHTTPX)
    monkeypatch.setattr(tracing, "RedisInstrumentor", _FakeRedis)
    monkeypatch.setattr(tracing, "SQLAlchemyInstrumentor", _FakeSQLAlchemy)
    monkeypatch.setattr(tracing, "CeleryInstrumentor", _FakeCelery)
    monkeypatch.setattr(tracing, "FastAPIInstrumentor", _FakeFastAPI)
    app = FastAPI()
    engine = create_engine("sqlite://")

    first = tracing.configure_tracing(app=app, engines=[engine, engine])
    second = tracing.configure_tracing(app=app, engines=[engine])

    assert first == second
    assert first.instrumented == frozenset({"fastapi", "sqlalchemy", "httpx", "redis", "celery"})
    assert len(_FakeHTTPX.calls) == 1
    assert callable(_FakeHTTPX.calls[0]["request_hook"])
    assert callable(_FakeHTTPX.calls[0]["async_request_hook"])
    assert len(_FakeRedis.calls) == 1
    assert len(_FakeSQLAlchemy.calls) == 1
    assert _FakeSQLAlchemy.calls[0]["engines"] == [engine]
    assert _FakeSQLAlchemy.calls[0]["enable_commenter"] is False
    assert len(_FakeCelery.calls) == 1
    assert len(_FakeFastAPI.calls) == 1
    assert callable(_FakeFastAPI.calls[0]["server_request_hook"])
    assert _FakeFastAPI.calls[0]["http_capture_headers_server_request"] == []
    assert _FakeFastAPI.calls[0]["http_capture_headers_server_response"] == []
    engine.dispose()


def test_external_fastapi_and_httpx_instrumentation_is_replaced_with_privacy_hooks(
    monkeypatch,
) -> None:
    provider = NoOpTracerProvider()
    app = FastAPI()
    app._is_instrumented_by_opentelemetry = True
    app.middleware_stack = object()  # type: ignore[assignment]
    _FakeFastAPI.calls = []
    _FakeFastAPI.uninstrumented = []
    _FakeHTTPX.calls = []
    _FakeHTTPX.active = True
    _FakeHTTPX.uninstrument_calls = 0
    monkeypatch.setattr(tracing, "FastAPIInstrumentor", _FakeFastAPI)
    monkeypatch.setattr(tracing, "HTTPXClientInstrumentor", _FakeHTTPX)
    monkeypatch.setattr(tracing, "_httpx_hooks_installed", False)

    tracing._instrument_app(app, provider)
    tracing._instrument_dependencies(provider=provider, engines=())

    assert _FakeFastAPI.uninstrumented == [app]
    assert app.middleware_stack is None
    assert callable(_FakeFastAPI.calls[0]["server_request_hook"])
    assert _FakeFastAPI.calls[0]["exclude_spans"] == ["receive", "send"]
    assert _FakeHTTPX.uninstrument_calls == 1
    assert callable(_FakeHTTPX.calls[0]["request_hook"])
    assert callable(_FakeHTTPX.calls[0]["async_request_hook"])


def test_an_external_provider_is_reused_without_a_second_exporter(monkeypatch) -> None:
    provider = NoOpTracerProvider()
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(
        tracing.trace,
        "set_tracer_provider",
        lambda _: pytest.fail("an external provider must not be replaced"),
    )
    monkeypatch.setattr(tracing, "_owned_provider", None)

    with capture_logs() as logs:
        selected, enabled = tracing._provider(
            service_name="service",
            environment="test",
            otlp_endpoint="http://collector.test:4317",
        )

    assert selected is provider
    assert enabled is False
    assert logs == [{"event": "otel_provider_managed_externally", "log_level": "info"}]


def test_owned_provider_is_flushed_and_shutdown_for_worker_exit(monkeypatch) -> None:
    calls = []

    class Provider:
        def force_flush(self, timeout_millis: int) -> bool:
            calls.append(("flush", timeout_millis))
            return True

        def shutdown(self) -> None:
            calls.append(("shutdown", None))

    monkeypatch.setattr(tracing, "_owned_provider", Provider())

    tracing.shutdown_owned_tracing(timeout_millis=123)

    assert calls == [("flush", 123), ("shutdown", None)]


def test_external_provider_is_flushed_but_not_shutdown_for_worker_exit(monkeypatch) -> None:
    calls = []

    class Provider:
        def force_flush(self, timeout_millis: int) -> bool:
            calls.append(("flush", timeout_millis))
            return True

        def shutdown(self) -> None:
            calls.append(("shutdown", None))

    provider = Provider()
    monkeypatch.setattr(tracing, "_owned_provider", None)
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)

    tracing.shutdown_owned_tracing(timeout_millis=456)

    assert calls == [("flush", 456)]
