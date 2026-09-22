"""Reproducible faults that do not disrupt a shared staging environment."""

from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import anthropic
import httpx
import httpx2
import pybreaker
import pytest
import redis
from prometheus_client import REGISTRY
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1 import caixa
from bancaemdia.cache.extracao_cache import ExtracaoCache
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao
from bancaemdia.observability.health import ReadinessChecker
from bancaemdia.observability.logging import UnhandledErrorMiddleware
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo
from bancaemdia.resilience.circuit_breaker import FAIL_MAX, new_anthropic_breaker
from bancaemdia.workers import celery_app, extraction, materialization

pytestmark = [pytest.mark.chaos, pytest.mark.xdist_group("postgres")]


def _extracao() -> dict[str, object]:
    bilhete = ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="Velez x Instituto",
        selecoes=[Selecao(mercado="Handicap", escolha="Instituto", odd=1.82)],
        odd_total=1.82,
        confianca=0.95,
    )
    return {
        "usuario_id": 0,
        "chat_id": 100,
        "message_id": 200,
        "postada_em": "2026-07-24T16:00:00",
        "versao_prompt": "extrair_bilhete_v3",
        "bilhete": bilhete.model_dump(mode="json"),
        "motivo": None,
        "grave": False,
        "cupons": [],
        "degrau": "BARATO",
        "custo_usd": 0.012,
        "nao_e_aposta": False,
    }


async def _materializar(engine: AsyncEngine, usuario: int, payload: dict[str, object]) -> None:
    await materialization.gravar_leitura(
        engine,
        usuario,
        materialization.ExtracaoDoJson.model_validate(payload),
        payload,
        "chaos-photo",
    )


async def _counts(engine: AsyncEngine, usuario: int) -> tuple[int, int]:
    async with AsyncSession(engine) as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(usuario)},
        )
        bets = await session.scalar(
            select(func.count())
            .select_from(models.Aposta)
            .where(models.Aposta.usuario_id == usuario)
        )
        events = await session.scalar(
            select(func.count())
            .select_from(models.Evento)
            .where(models.Evento.usuario_id == usuario)
        )
    return int(bets or 0), int(events or 0)


def test_worker_loss_is_requeued_and_dlq_is_reserved_for_terminal_failure() -> None:
    assert celery_app.app.conf.task_acks_late is True
    assert celery_app.app.conf.task_reject_on_worker_lost is True
    assert celery_app.DEAD_LETTER_QUEUE not in celery_app.app.amqp.queues.consume_from


def test_anthropic_timeout_is_retried_before_dead_letter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def timeout(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))

    monkeypatch.setattr(extraction, "ler_mensagem", timeout)
    result = extraction.extrair_bilhete_task.apply(
        kwargs={"usuario_id": 7, "imagem_base64": base64.b64encode(b"photo").decode()}
    )
    assert isinstance(result.result, anthropic.APITimeoutError)
    assert len(calls) == 1 + extraction.extrair_bilhete_task.max_retries


def test_five_anthropic_5xx_failures_open_breaker_until_recovery() -> None:
    breaker = new_anthropic_breaker()
    calls = []

    def fail() -> None:
        calls.append(1)
        raise anthropic.InternalServerError(
            "provider unavailable",
            response=httpx2.Response(
                503, request=httpx2.Request("POST", "https://api.anthropic.com")
            ),
            body=None,
        )

    for _ in range(FAIL_MAX - 1):
        with pytest.raises(anthropic.InternalServerError):
            breaker.call(fail)
    with pytest.raises(pybreaker.CircuitBreakerError):
        breaker.call(fail)
    with pytest.raises(pybreaker.CircuitBreakerError):
        breaker.call(fail)
    assert len(calls) == FAIL_MAX
    breaker.reset_timeout = 0
    assert breaker.call(lambda: "recovered") == "recovered"
    assert breaker.current_state == pybreaker.STATE_CLOSED


def test_redis_outage_pauses_paid_extraction_until_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class Limiter:
        def acquire(self, user_id: int) -> None:
            raise redis.ConnectionError("redis unavailable")

    class Reader:
        modelo_escalonamento = "unused"

        def ler(self, *args: object, **kwargs: object) -> None:
            calls.append(1)

    monkeypatch.setattr(extraction, "get_limiter", Limiter)
    monkeypatch.setattr(extraction, "get_leitor", Reader)
    monkeypatch.setattr(extraction, "get_cache", lambda: None)
    result = extraction.extrair_bilhete_task.apply(
        kwargs={"usuario_id": 7, "imagem_base64": base64.b64encode(b"photo").decode()}
    )
    assert isinstance(result.result, redis.ConnectionError)
    assert calls == []


def test_cache_miss_during_redis_outage_does_not_crash_extraction() -> None:
    class Down:
        def get(self, key: str) -> None:
            raise redis.ConnectionError("down")

    assert ExtracaoCache(Down()).buscar("photo") is None


def test_disk_full_emits_the_alert_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    async def full(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)
        raise OperationalError("INSERT", {}, OSError(errno.ENOSPC, "disk full"))

    monkeypatch.setattr(materialization, "gravar_leitura", full)
    monkeypatch.setattr(materialization, "get_engine", lambda: None)
    before = REGISTRY.get_sample_value("materialization_failed_total", {"reason": "disk_full"}) or 0
    with pytest.raises(OperationalError):
        materialization.materializar_aposta(7, _extracao())
    after = REGISTRY.get_sample_value("materialization_failed_total", {"reason": "disk_full"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_primary_failover_returns_503_then_recovers() -> None:
    healthy = False

    async def primary() -> dict[str, str]:
        await asyncio.sleep(0)
        if not healthy:
            raise OperationalError("SELECT 1", {}, OSError("connection lost"))
        return {"mode": "writable"}

    checker = ReadinessChecker(
        {"postgres_primary": primary}, timeout_seconds=1, cache_ttl_seconds=0
    )
    assert (await checker.check()).status_code == 503
    healthy = True
    assert (await checker.check()).status_code == 200

    async def failing_app(scope, receive, send) -> None:
        await asyncio.sleep(0)
        raise OperationalError("INSERT", {}, OSError("disk full"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=UnhandledErrorMiddleware(failing_app)),
        base_url="http://test",
    ) as client:
        response = await client.get("/")
    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


@pytest.mark.asyncio
async def test_disk_full_rolls_back_every_write_then_retry_is_idempotent(
    engine_app: AsyncEngine, novo_usuario, monkeypatch: pytest.MonkeyPatch
) -> None:
    usuario = await novo_usuario()
    payload = _extracao()
    original = EventoRepo.append

    async def fail_after_insert(self, session, data):
        await original(self, session, data)
        raise OperationalError("INSERT", {}, OSError("No space left on device"))

    with monkeypatch.context() as fault:
        fault.setattr(EventoRepo, "append", fail_after_insert)
        with pytest.raises(OperationalError):
            await _materializar(engine_app, usuario, payload)
    assert await _counts(engine_app, usuario) == (0, 0)

    await _materializar(engine_app, usuario, payload)
    before = await _counts(engine_app, usuario)
    await _materializar(engine_app, usuario, payload)
    assert await _counts(engine_app, usuario) == before
    assert before[0] == 1
    assert (await reconstruir_usuario(usuario, engine=engine_app, dry_run=True)).alteradas == 0


@pytest.mark.asyncio
async def test_lost_http_response_retries_one_cash_movement(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como, novo_usuario
) -> None:
    usuario = await novo_usuario()
    async with engine_admin.begin() as connection:
        house_id = await connection.scalar(
            insert(models.Casa).values(nome=f"Chaos {uuid4().hex}").returning(models.Casa.id)
        )
    async with como(engine_app, usuario) as session:
        account = await ContaCasaRepo().create(
            session, {"usuario_id": usuario, "casa_id": house_id, "apelido": "chaos"}
        )
        await session.commit()
    key = f"chaos:{uuid4()}"
    payload = caixa.MovimentoNovo.model_validate({
        "tipo": "deposito",
        "valor_centavos": 100_000,
        "conta_casa_id": account.id,
        "ocorrido_em": datetime(2026, 9, 20, 12, tzinfo=UTC),
    })

    async def submit():
        async with como(engine_app, usuario) as session:
            owner = await UsuarioRepo().get_by_id(session, usuario)
            assert owner is not None
            return await caixa.registrar_movimento(payload, owner, session, key)

    async def disconnected_client() -> None:
        await submit()  # Commit completed, but the response never reached the client.
        raise ConnectionResetError("response lost")

    with pytest.raises(ConnectionResetError):
        await disconnected_client()
    replay = await submit()
    assert replay.status_code == 201
    async with como(engine_app, usuario) as session:
        assert await session.scalar(select(func.count()).select_from(models.Movimento)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(models.MovimentoRequisicao)) == 1
        )
    assert (await reconstruir_usuario(usuario, engine=engine_app, dry_run=True)).alteradas == 0


@pytest.mark.asyncio
async def test_sigkill_mid_materialization_rolls_back_and_redelivery_is_safe(
    engine_app: AsyncEngine, banco, novo_usuario
) -> None:
    usuario = await novo_usuario()
    payload = _extracao()
    child = """
import asyncio, json, os
from sqlalchemy.ext.asyncio import create_async_engine
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.workers import materialization

original = EventoRepo.append
async def interrupt(self, session, data):
    await original(self, session, data)
    print('IN_TRANSACTION', flush=True)
    await asyncio.Event().wait()
EventoRepo.append = interrupt

async def main():
    engine = create_async_engine(os.environ['CHAOS_DATABASE_URL'])
    payload = json.loads(os.environ['CHAOS_PAYLOAD'])
    await materialization.gravar_leitura(
        engine, int(os.environ['CHAOS_USER_ID']),
        materialization.ExtracaoDoJson.model_validate(payload), payload, 'chaos-photo'
    )
asyncio.run(main())
"""
    environment = os.environ.copy()
    environment.update(
        CHAOS_DATABASE_URL=banco.url_app,
        CHAOS_PAYLOAD=json.dumps(payload),
        CHAOS_USER_ID=str(usuario),
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", child],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        assert process.stdout is not None
        marker = pool.submit(process.stdout.readline).result(timeout=20)
        assert marker.strip() == "IN_TRANSACTION", marker
    finally:
        process.kill()
        process.communicate(timeout=10)
        pool.shutdown(wait=False)
    assert await _counts(engine_app, usuario) == (0, 0)
    await _materializar(engine_app, usuario, payload)
    assert (await _counts(engine_app, usuario))[0] == 1
    assert (await reconstruir_usuario(usuario, engine=engine_app, dry_run=True)).alteradas == 0
