"""Synthetic secrets must not reach logs, traces, errors or DLQ summaries."""

import io
import logging
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from bancaemdia.config import get_settings
from bancaemdia.integrations.telegram.client import TelegramApiError, TelegramClient
from bancaemdia.observability.logging import configure_logging
from bancaemdia.workers.celery_app import send_to_dead_letter


@pytest.mark.parametrize(
    "path", ["botSENTINEL-token/sendMessage", "file/botSENTINEL-token/photo.jpg"]
)
def test_http_log_redacts_embedded_bot_token(path):
    stream = io.StringIO()
    configure_logging(stream=stream)
    logging.getLogger("httpx").info(
        'HTTP Request: GET https://api.telegram.org/%s "HTTP/1.1 500"', path
    )
    assert "SENTINEL" not in stream.getvalue()
    assert "REDACTED" in stream.getvalue()


async def test_client_exception_report_and_http_spans_have_no_secret():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def fail(request):
        raise httpx.ReadTimeout("SENTINEL-message-photo", request=request)

    client = TelegramClient("SENTINEL-token", transport=httpx.MockTransport(fail))
    HTTPXClientInstrumentor.instrument_client(client._client, tracer_provider=provider)
    message = "SENTINEL-content"
    try:
        with pytest.raises(TelegramApiError) as error:
            await client.send_message(123, message)
        rendered = "".join(traceback.format_exception(error.value))
        assert "SENTINEL" not in rendered
        assert not exporter.get_finished_spans()
    finally:
        HTTPXClientInstrumentor.uninstrument_client(client._client)
        await client.aclose()
        provider.shutdown()


def test_telegram_dlq_has_safe_error_code_and_no_exception_text():
    sender = SimpleNamespace(
        name="telegram.tick", request=SimpleNamespace(is_eager=False, delivery_info={}), app=Mock()
    )
    send_to_dead_letter(sender, "synthetic-task", RuntimeError("SENTINEL-token-photo"), (), {})
    kwargs = sender.app.send_task.call_args.kwargs
    assert kwargs["headers"]["exception"] == "telegram_task_failed"
    assert "SENTINEL" not in str(kwargs)


def test_positive_configured_limits_are_required():
    settings = get_settings()
    values = settings.model_dump()
    values["TELEGRAM_ACTION_LIMITS"] = {"photo": (0, 1, 2)}
    with pytest.raises(ValueError, match="all four actions"):
        type(settings)(**values)
