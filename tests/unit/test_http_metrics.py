from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest

from bancaemdia.observability import metrics
from bancaemdia.observability.metrics import (
    UNMATCHED_HTTP_PATH,
    create_http_metrics,
    instrument_http_metrics,
)


def test_http_metrics_use_the_route_template_and_exact_status() -> None:
    registry = CollectorRegistry()
    http_metrics = create_http_metrics(registry)
    app = FastAPI()
    instrument_http_metrics(app, http_metrics)

    @app.get("/things/{thing_id}")
    async def thing(thing_id: str) -> Response:
        return Response(status_code=201)

    client = TestClient(app)
    assert client.get("/things/secret-1").status_code == 201
    assert client.get("/things/secret-2").status_code == 201

    labels = {"method": "GET", "path": "/things/{thing_id}", "status": "201"}
    assert registry.get_sample_value("http_requests_total", labels) == 2
    assert registry.get_sample_value("http_request_duration_seconds_count", labels) == 2
    exposition = generate_latest(registry).decode()
    assert "secret-1" not in exposition
    assert "secret-2" not in exposition


def test_unmatched_urls_share_one_bounded_path_label() -> None:
    registry = CollectorRegistry()
    http_metrics = create_http_metrics(registry)
    app = FastAPI()
    instrument_http_metrics(app, http_metrics)
    client = TestClient(app)

    assert client.get("/not-found/customer-123").status_code == 404
    assert client.get("/also-not-found/customer-456").status_code == 404

    labels = {"method": "GET", "path": UNMATCHED_HTTP_PATH, "status": "404"}
    assert registry.get_sample_value("http_requests_total", labels) == 2
    exposition = generate_latest(registry).decode()
    assert "customer-123" not in exposition
    assert "customer-456" not in exposition


def test_uncaught_exception_is_counted_as_status_500() -> None:
    registry = CollectorRegistry()
    http_metrics = create_http_metrics(registry)
    app = FastAPI()
    instrument_http_metrics(app, http_metrics)

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).get("/explode")

    assert response.status_code == 500
    labels = {"method": "GET", "path": "/explode", "status": "500"}
    assert registry.get_sample_value("http_requests_total", labels) == 1
    assert registry.get_sample_value("http_request_duration_seconds_count", labels) == 1


def test_http_instrumentation_is_idempotent_for_each_app() -> None:
    registry = CollectorRegistry()
    http_metrics = create_http_metrics(registry)
    app = FastAPI()

    first = instrument_http_metrics(app, http_metrics)
    second = instrument_http_metrics(app, http_metrics)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    assert first is second
    assert TestClient(app).get("/health").status_code == 200
    labels = {"method": "GET", "path": "/health", "status": "200"}
    assert registry.get_sample_value("http_requests_total", labels) == 1
    assert registry.get_sample_value("http_request_duration_seconds_count", labels) == 1


def test_separate_test_registries_do_not_collide_or_share_samples() -> None:
    first_registry = CollectorRegistry()
    second_registry = CollectorRegistry()

    first = create_http_metrics(first_registry)
    second = create_http_metrics(second_registry)
    labels = {"method": "POST", "path": "/api/v1/apostas", "status": "201"}
    first.requests.labels(**labels).inc()

    assert first_registry.get_sample_value("http_requests_total", labels) == 1
    assert second_registry.get_sample_value("http_requests_total", labels) is None
    assert first.requests is not second.requests


def test_business_metrics_have_only_the_declared_low_cardinality_labels() -> None:
    apostas_labels = {"origem": "manual", "estado": "PENDENTE"}
    movimentos_labels = {"tipo": "DEPOSITO"}
    apostas_before = metrics.REGISTRY.get_sample_value("apostas_created_total", apostas_labels) or 0
    movimentos_before = (
        metrics.REGISTRY.get_sample_value("caixa_movimentos_total", movimentos_labels) or 0
    )

    metrics.apostas_created_total.labels(**apostas_labels).inc()
    metrics.caixa_movimentos_total.labels(**movimentos_labels).inc()

    assert (
        metrics.REGISTRY.get_sample_value("apostas_created_total", apostas_labels)
        == apostas_before + 1
    )
    assert (
        metrics.REGISTRY.get_sample_value("caixa_movimentos_total", movimentos_labels)
        == movimentos_before + 1
    )


def test_exact_system_metric_aliases_are_exported() -> None:
    exposition = generate_latest(metrics.metrics_registry()).decode()

    assert "# TYPE process_cpu_seconds_total counter" in exposition
    assert exposition.count("# HELP process_cpu_seconds_total ") == 1
    assert "# TYPE process_memory_bytes gauge" in exposition
