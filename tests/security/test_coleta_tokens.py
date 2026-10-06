import io
import json
import logging

import pytest
import structlog
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from bancaemdia.config import get_settings
from bancaemdia.main import app
from bancaemdia.middleware.coleta_credentials import CollectionCredentialMiddleware
from bancaemdia.observability.logging import configure_logging
from bancaemdia.observability.tracing import _sanitize_server_request
from bancaemdia.services.coleta_tokens import digest


@pytest.mark.parametrize(
    "path",
    [
        "/coleta",
        "/api/v1/coleta",
        "/api/v1/coleta/pairing-codes",
        "/api/v1/coleta/pairing-exchange",
        "/api/v1/coleta/status",
    ],
)
def test_https_is_required_before_credentials_or_forwarded_headers_are_used(path):
    response = TestClient(app, base_url="http://api.test").post(
        path, headers={"X-Coleta-Token": "cti_SENTINEL", "X-Forwarded-Proto": "https"}
    )
    assert response.status_code == 400 and response.json() == {"detail": "HTTPS required"}
    assert "Location" not in response.headers and "SENTINEL" not in response.text


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_credential_bearing_redirects_never_leave_the_server(status):
    test_app = FastAPI()

    @test_app.get("/redirect")
    def redirect():
        return RedirectResponse("https://untrusted.test", status_code=status)

    test_app.add_middleware(CollectionCredentialMiddleware)
    response = TestClient(test_app, base_url="https://api.test", follow_redirects=False).get(
        "/redirect", headers={"X-Coleta-Token": "cti_SENTINEL"}
    )
    assert response.status_code == 400 and "location" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {"codigo": "cpc_SENTINEL", "instalacao_publica_id": "invalid"},
        {
            "codigo": {"secret": "cpc_SENTINEL"},
            "instalacao_publica_id": "91b643c0-46e6-4b1b-b488-6254247128fd",
        },
        {
            "codigo": "cpc_SENTINEL",
            "instalacao_publica_id": "91b643c0-46e6-4b1b-b488-6254247128fd",
            "fingerprint": "SENTINEL",
        },
    ],
)
def test_exchange_validation_never_echoes_the_code_or_accepts_fingerprints(body):
    response = TestClient(app, base_url="https://api.test").post(
        "/api/v1/coleta/pairing-exchange", json=body
    )
    assert response.status_code == 422 and response.json() == {"detail": "Invalid pairing request"}
    assert "SENTINEL" not in response.text


def test_code_lifetime_is_bounded_by_settings_and_domain_separation_is_keyed():
    settings = get_settings()
    with pytest.raises(ValueError):
        type(settings)(**{**settings.model_dump(), "COLETA_PAIRING_TTL_SECONDS": 1801})
    assert len(digest("cti_synthetic")) == 64
    assert digest("same", "token") != digest("same", "pairing")


def test_credentials_are_removed_from_logs_exception_reports_and_audit_diffs():
    stream = io.StringIO()
    configure_logging(stream=stream)
    token = "cti_SENTINEL"
    structlog.get_logger().info(
        "credential example " + token, diff={"codigo": "plain-SENTINEL", "token": token}
    )
    try:
        raise ValueError(token)
    except ValueError:
        logging.getLogger("example").exception("credential request failed")
    assert "SENTINEL" not in stream.getvalue()
    for line in stream.getvalue().splitlines():
        assert json.loads(line)["message"]


def test_otel_request_attributes_do_not_export_codes_in_paths_or_queries():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("GET") as span:
        _sanitize_server_request(
            span,
            {
                "type": "http",
                "scheme": "https",
                "server": ("api.test", 443),
                "path": "/coleta/cti_SENTINEL",
                "query_string": b"codigo=cpc_SENTINEL",
                "headers": [],
            },
        )
    assert "SENTINEL" not in str(exporter.get_finished_spans()[0].attributes)
    provider.shutdown()
