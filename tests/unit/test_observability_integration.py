from __future__ import annotations

import ast
import asyncio
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_fastapi_instrumentator.middleware import PrometheusInstrumentatorMiddleware

from bancaemdia import main
from bancaemdia.api.v1 import apostas, caixa, coleta, upload
from bancaemdia.auth.middleware import JWTAuthMiddleware
from bancaemdia.domain.registros import Movimento
from bancaemdia.middleware.rate_limit import AuthRateLimitMiddleware, RateLimitMiddleware
from bancaemdia.middleware.rls import RLSMiddleware
from bancaemdia.middleware.router import RouterMiddleware
from bancaemdia.observability.health import (
    ReadinessChecker,
    check_anthropic,
    check_celery_queue_depth,
)
from bancaemdia.observability.logging import (
    RequestIdMiddleware,
    UnhandledErrorMiddleware,
    UserLogContextMiddleware,
)
from bancaemdia.security.http import EndpointBodyLimitMiddleware, SecurityHeadersMiddleware
from bancaemdia.workers import extraction, materialization, pairing
from bancaemdia.workers import upload as upload_worker


class RecordingCounter:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def labels(self, **labels: str) -> RecordingCounter:
        self.events.append(("labels", labels))
        return self

    def inc(self, amount: float = 1) -> None:
        self.events.append(("inc", amount))


class RecordingSession:
    def __init__(self, counter: RecordingCounter, *, fail_commit: bool = False) -> None:
        self.counter = counter
        self.fail_commit = fail_commit
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        # A counter increment before this point would report data that can still be rolled back.
        assert self.counter.events == []
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")

    async def rollback(self) -> None:
        self.rollbacks += 1


def _client() -> TestClient:
    # Do not enter the context manager: doing so starts the production lifespan and probes a real DB.
    return TestClient(main.app, raise_server_exceptions=False)


def test_main_wires_observability_around_auth_and_domain_middleware() -> None:
    layers = [layer.cls for layer in main.app.user_middleware]

    assert (
        layers.index(SecurityHeadersMiddleware)
        < layers.index(RequestIdMiddleware)
        < layers.index(UnhandledErrorMiddleware)
        < layers.index(PrometheusInstrumentatorMiddleware)
        < layers.index(AuthRateLimitMiddleware)
        < layers.index(JWTAuthMiddleware)
        < layers.index(UserLogContextMiddleware)
        < layers.index(RateLimitMiddleware)
        < layers.index(RLSMiddleware)
        < layers.index(RouterMiddleware)
        < layers.index(EndpointBodyLimitMiddleware)
    )
    assert getattr(main.app, "_is_instrumented_by_opentelemetry", False) is True


async def test_lifespan_runs_the_replica_lag_monitor_and_cleans_it_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Connection:
        async def execute(self, statement: object) -> None:
            events.append("primary_ready")

    class Begin:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *args: object) -> None:
            return None

    class Engine:
        def __init__(self, name: str) -> None:
            self.name = name

        def begin(self) -> Begin:
            return Begin()

        async def dispose(self) -> None:
            events.append(f"{self.name}_disposed")

    class Monitor:
        async def run(self) -> None:
            events.append("monitor_started")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("monitor_stopped")

    monkeypatch.setattr(main, "engine", Engine("primary"))
    monkeypatch.setattr(main, "replica_engine", Engine("replica"))
    monkeypatch.setattr(main, "replica_lag_monitor", Monitor())

    async with main.app.router.lifespan_context(main.app):
        await asyncio.sleep(0)
        assert events == ["primary_ready", "monitor_started"]

    assert events == [
        "primary_ready",
        "monitor_started",
        "monitor_stopped",
        "primary_disposed",
        "replica_disposed",
    ]


def test_health_is_dependency_free_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    def readiness_must_not_run() -> None:
        raise AssertionError("liveness called readiness")

    monkeypatch.setattr(main, "get_readiness_checker", readiness_must_not_run)
    monkeypatch.setattr(
        main,
        "breaker_states",
        lambda: (_ for _ in ()).throw(AssertionError("liveness queried Redis")),
    )

    response = _client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_exposes_a_failed_report_as_503_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "status": "not_ready",
        "checks": {"postgres_primary": {"status": "failed", "details": {"reason": "down"}}},
    }

    class Report:
        status_code = 503

        def as_dict(self) -> dict[str, object]:
            return payload

    class Checker:
        async def check(self) -> Report:
            return Report()

    monkeypatch.setattr(main, "get_readiness_checker", Checker)

    response = _client().get("/ready")

    assert response.status_code == 503
    assert response.json() == payload
    assert "WWW-Authenticate" not in response.headers


async def _available_dependency() -> dict[str, object]:
    await asyncio.sleep(0)
    return {"mode": "available"}


def _checker_with_report_only(name: str, check: Any) -> ReadinessChecker:
    return ReadinessChecker(
        {
            "postgres_primary": _available_dependency,
            "postgres_replica": _available_dependency,
            "redis": _available_dependency,
            name: check,
        },
        timeout_seconds=0.5,
        report_only={name},
    )


def _assert_report_only_degradation(response: Any, component_name: str, reason: str) -> None:
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    component = payload["checks"][component_name]
    assert component["status"] == "degraded"
    assert component["impact"] == "report_only"
    assert component["details"]["reason"] == reason


def test_anthropic_without_a_key_is_report_only_for_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    async def anthropic_check() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.anthropic.test/",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await check_anthropic(client, api_key=None)

    checker = _checker_with_report_only("anthropic", anthropic_check)
    monkeypatch.setattr(main, "get_readiness_checker", lambda: checker)

    response = _client().get("/ready")

    _assert_report_only_degradation(response, "anthropic", "not_configured")
    assert requests == 0


def test_anthropic_outage_is_report_only_and_redacted_from_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "must-never-appear"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=f"upstream echoed {secret}")

    async def anthropic_check() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.anthropic.test/",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await check_anthropic(client, api_key=secret)

    checker = _checker_with_report_only("anthropic", anthropic_check)
    monkeypatch.setattr(main, "get_readiness_checker", lambda: checker)

    response = _client().get("/ready")

    _assert_report_only_degradation(response, "anthropic", "provider_error")
    assert response.json()["checks"]["anthropic"]["details"]["http_status"] == 503
    assert secret not in response.text


def test_celery_backlog_is_report_only_for_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    class BackloggedRedis:
        async def llen(self, key: str) -> int:
            return 5 if key == "extraction" else 0

    async def celery_check() -> dict[str, object]:
        return await check_celery_queue_depth(
            BackloggedRedis(),  # type: ignore[arg-type]
            queues=("extraction",),
            limit=5,
        )

    checker = _checker_with_report_only("celery_queue_depth", celery_check)
    monkeypatch.setattr(main, "get_readiness_checker", lambda: checker)

    response = _client().get("/ready")

    _assert_report_only_degradation(response, "celery_queue_depth", "threshold_exceeded")
    assert response.json()["checks"]["celery_queue_depth"]["details"] == {
        "reason": "threshold_exceeded",
        "depths": {"extraction": 5},
        "total": 5,
        "limit": 5,
    }


async def _prepare_manual_bet(
    monkeypatch: pytest.MonkeyPatch, counter: RecordingCounter, session: RecordingSession
) -> None:
    async def no_unit(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)
        return None

    async def house_id(*args: object, **kwargs: object) -> int:
        await asyncio.sleep(0)
        return 11

    async def no_account(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)
        return None

    async def applied(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0)
        return object()

    monkeypatch.setattr(apostas, "apostas_created_total", counter)
    monkeypatch.setattr(apostas, "casa_canonica", lambda value: value)
    monkeypatch.setattr(apostas.UnidadeRepo, "get_vigente", no_unit)
    monkeypatch.setattr(apostas.CasaRepo, "get_id_by_nome", house_id)
    from bancaemdia.domain.account_attribution import AccountResolution, ResolutionStatus

    async def no_attribution(*args: object, **kwargs: object) -> AccountResolution:
        await asyncio.sleep(0)
        return AccountResolution(ResolutionStatus.NONE)

    monkeypatch.setattr(apostas, "attribute_account", no_attribution)
    monkeypatch.setattr(apostas, "sync_account_review", no_account)
    monkeypatch.setattr(apostas, "_aplicar", applied)
    monkeypatch.setattr(
        apostas,
        "projetar",
        lambda events: ({"estado": "PENDENTE", "stake_unidades": 1, "freebet": False}, []),
    )
    monkeypatch.setattr(apostas, "linha_da_aposta", lambda *args: {"chave": "m:test"})

    await apostas.criar_aposta(
        apostas.ApostaManual(casa="Betano", odd=2, stake_unidades=1),
        SimpleNamespace(id=7),
        session,  # type: ignore[arg-type]
    )


async def test_manual_bet_metric_is_emitted_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = RecordingCounter()
    session = RecordingSession(counter)

    await _prepare_manual_bet(monkeypatch, counter, session)

    assert session.commits == 1
    assert counter.events == [
        ("labels", {"origem": "manual", "estado": "PENDENTE"}),
        ("inc", 1),
    ]


async def test_manual_bet_metric_is_not_emitted_when_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = RecordingCounter()
    session = RecordingSession(counter, fail_commit=True)

    with pytest.raises(RuntimeError, match="commit failed"):
        await _prepare_manual_bet(monkeypatch, counter, session)

    assert counter.events == []


async def _register_adjustment(
    monkeypatch: pytest.MonkeyPatch, counter: RecordingCounter, session: RecordingSession
) -> None:
    async def locked(*args: object, **kwargs: object) -> bool:
        await asyncio.sleep(0)
        return True

    async def append_movement(
        self: object, session_arg: object, values: dict[str, Any]
    ) -> Movimento:
        await asyncio.sleep(0)
        return Movimento(
            id=12,
            usuario_id=int(values["usuario_id"]),
            conta_casa_id=None,
            tipo=str(values["tipo"]),
            valor_centavos=int(values["valor_centavos"]),
            ocorrido_em=values["ocorrido_em"],
            descricao=values["descricao"],
        )

    async def append_event(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0)
        return object()

    async def no_previous_request(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)
        return None

    async def append_request(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0)
        return object()

    monkeypatch.setattr(caixa, "caixa_movimentos_total", counter)
    monkeypatch.setattr(caixa, "_travar", locked)
    monkeypatch.setattr(caixa, "_travar_idempotencia", locked)
    monkeypatch.setattr(caixa.MovimentoRepo, "append", append_movement)
    monkeypatch.setattr(caixa.EventoRepo, "append", append_event)
    monkeypatch.setattr(caixa.MovimentoRequisicaoRepo, "get", no_previous_request)
    monkeypatch.setattr(caixa.MovimentoRequisicaoRepo, "append", append_request)

    await caixa.registrar_movimento(
        caixa.MovimentoNovo(
            tipo="AJUSTE",
            valor_centavos=250,
            ocorrido_em=datetime(2026, 9, 21, 12, 0),
        ),
        SimpleNamespace(id=7),
        session,  # type: ignore[arg-type]
        "observability-adjustment",
    )


async def test_cash_movement_metric_is_emitted_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = RecordingCounter()
    session = RecordingSession(counter)

    await _register_adjustment(monkeypatch, counter, session)

    assert session.commits == 1
    assert counter.events == [("labels", {"tipo": "AJUSTE"}), ("inc", 1)]


async def test_cash_movement_metric_is_not_emitted_when_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = RecordingCounter()
    session = RecordingSession(counter, fail_commit=True)

    with pytest.raises(RuntimeError, match="commit failed"):
        await _register_adjustment(monkeypatch, counter, session)

    assert session.rollbacks == 1
    assert counter.events == []


def _literal_custom_spans(module: ModuleType) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "custom_span"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def test_custom_spans_are_wired_into_the_real_pipeline_paths() -> None:
    expected = {
        upload: {"upload.parse"},
        upload_worker: {"upload.parse"},
        extraction: {"extraction.chamar_anthropic"},
        materialization: {"materializacao.upsert"},
        pairing: {"cruzamento.pareador"},
        coleta: {"coleta.casa"},
    }

    for module, required_spans in expected.items():
        assert required_spans <= _literal_custom_spans(module)
