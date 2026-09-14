from __future__ import annotations

import base64
import json
import threading
from datetime import datetime

import anthropic
import pybreaker
import pytest
from celery import signals
from celery.utils.time import get_exponential_backoff_interval

from bancaemdia.cache.extracao_cache import ExtracaoCache
from bancaemdia.extracao import cliente
from bancaemdia.extracao.cliente import VERSAO_PROMPT, Leitura
from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao
from bancaemdia.workers import celery_app, extraction


def _bom():
    return ExtracaoBilhete(
        casa="Betano",
        tipo="simples",
        evento="Velez x Instituto",
        selecoes=[Selecao(mercado="Handicap", escolha="Instituto", odd=1.82)],
        odd_total=1.82,
        confianca=0.95,
    )


def _leitor():
    class Leitor:
        modelo_escalonamento = "claude-sonnet-5"

        def __init__(self):
            self.chamadas = []

        def ler(self, imagem, tipo="image/jpeg", *, legenda="", postada_em=None, escalonar=False):
            self.chamadas.append((imagem, tipo, legenda, postada_em, escalonar))
            return Leitura((_bom(),), "claude-haiku-4-5", custo_usd=0.012)

    return Leitor()


def _cache():
    class Redis:
        def __init__(self):
            self.dados = {}

        def get(self, nome):
            return self.dados.get(nome)

        def set(self, nome, valor, ex=None, nx=False):
            if nx and nome in self.dados:
                return None
            self.dados[nome] = valor.encode()
            return True

    return ExtracaoCache(Redis())


def _limitador(eventos):
    class Limitador:
        def acquire(self, user_id, tokens=1):
            eventos.append(("acquire", user_id, tokens))
            return 0.0

    return Limitador()


def _argumentos():
    return {
        "usuario_id": 7,
        "imagem_base64": base64.b64encode(b"foto").decode("ascii"),
        "nome_do_arquivo": "photo_12@24-07-2026.png",
        "legenda": "1u https://www.betano.bet.br/x",
        "postada_em": "2026-07-24T16:00:00",
        "casas_do_link": ["Betano"],
        "odds_do_texto": [1.82],
        "chat_id": 100,
        "message_id": 200,
    }


def test_task_is_routed_to_the_extraction_queue() -> None:
    task = extraction.extrair_bilhete_task

    assert task.name == "extraction.extrair_bilhete"
    assert celery_app.app.amqp.router.route({}, task.name)["queue"].name == "extraction"


def test_worker_imports_the_extraction_tasks() -> None:
    assert "bancaemdia.workers.extraction" in celery_app.app.conf.include


def test_task_retries_transient_provider_failures() -> None:
    task = extraction.extrair_bilhete_task

    assert set(task.autoretry_for) == {
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
        anthropic.OverloadedError,
        anthropic.ServiceUnavailableError,
        anthropic.DeadlineExceededError,
        pybreaker.CircuitBreakerError,
    }
    assert task.max_retries == 3


def test_retries_wait_longer_than_an_open_breaker() -> None:
    task = extraction.extrair_bilhete_task

    esperas = [
        get_exponential_backoff_interval(
            factor=int(task.retry_backoff),
            retries=tentativa,
            maximum=task.retry_backoff_max,
            full_jitter=task.retry_jitter,
        )
        for tentativa in range(task.max_retries)
    ]

    assert esperas == [60, 120, 240]
    assert esperas[0] >= cliente.anthropic_breaker.reset_timeout


def test_extrair_bilhete_reads_the_image_and_returns_json(monkeypatch) -> None:
    leitor = _leitor()
    monkeypatch.setattr(extraction, "get_leitor", lambda: leitor)
    monkeypatch.setattr(extraction, "get_cache", _cache)
    monkeypatch.setattr(extraction, "get_limiter", lambda: _limitador([]))

    resultado = extraction.extrair_bilhete(**_argumentos())

    assert leitor.chamadas == [
        (b"foto", "image/png", "1u https://www.betano.bet.br/x", datetime(2026, 7, 24, 16), False)
    ]
    assert json.loads(json.dumps(resultado)) == {
        "usuario_id": 7,
        "chat_id": 100,
        "message_id": 200,
        "versao_prompt": VERSAO_PROMPT,
        "bilhete": _bom().model_dump(mode="json"),
        "motivo": None,
        "grave": False,
        "cupons": [],
        "degrau": "BARATO",
        "custo_usd": 0.012,
        "nao_e_aposta": False,
    }


def test_extrair_bilhete_defaults_to_no_date_and_no_hints(monkeypatch) -> None:
    leitor = _leitor()
    monkeypatch.setattr(extraction, "get_leitor", lambda: leitor)
    monkeypatch.setattr(extraction, "get_cache", _cache)
    monkeypatch.setattr(extraction, "get_limiter", lambda: _limitador([]))

    resultado = extraction.extrair_bilhete(7, base64.b64encode(b"foto").decode("ascii"))

    assert leitor.chamadas == [(b"foto", "image/jpeg", "", None, False)]
    assert resultado["chat_id"] is None


def test_same_image_sent_again_comes_from_the_cache(monkeypatch) -> None:
    leitor = _leitor()
    cache = _cache()
    monkeypatch.setattr(extraction, "get_leitor", lambda: leitor)
    monkeypatch.setattr(extraction, "get_cache", lambda: cache)
    monkeypatch.setattr(extraction, "get_limiter", lambda: _limitador([]))

    primeira = extraction.extrair_bilhete(**_argumentos())
    segunda = extraction.extrair_bilhete(**{**_argumentos(), "chat_id": 999, "message_id": 1})

    assert len(leitor.chamadas) == 1
    assert (primeira["degrau"], segunda["degrau"]) == ("BARATO", "CACHE")
    assert segunda["custo_usd"] == pytest.approx(0.0)
    assert segunda["bilhete"] == primeira["bilhete"]


def test_each_paid_reading_takes_a_rate_limit_token_first(monkeypatch) -> None:
    eventos = []
    cache = _cache()

    class Leitor:
        modelo_escalonamento = "claude-sonnet-5"

        def ler(self, imagem, tipo="image/jpeg", *, legenda="", postada_em=None, escalonar=False):
            eventos.append(("ler", escalonar))
            if escalonar:
                return Leitura((_bom(),), "claude-sonnet-5", custo_usd=0.06)
            ruim = _bom().model_copy(update={"odd_total": 9.9})
            return Leitura((ruim,), "claude-haiku-4-5", custo_usd=0.012)

    monkeypatch.setattr(extraction, "get_leitor", Leitor)
    monkeypatch.setattr(extraction, "get_cache", lambda: cache)
    monkeypatch.setattr(extraction, "get_limiter", lambda: _limitador(eventos))

    primeira = extraction.extrair_bilhete(**_argumentos())
    segunda = extraction.extrair_bilhete(**_argumentos())

    assert (primeira["degrau"], segunda["degrau"]) == ("CARO", "CACHE")
    assert eventos == [("acquire", 7, 1), ("ler", False), ("acquire", 7, 1), ("ler", True)]


def test_worker_ready_clears_old_prompt_versions_in_the_background(monkeypatch) -> None:
    feito = threading.Event()

    class Cache:
        def limpar_versoes_antigas(self):
            feito.set()
            return 0

    monkeypatch.setattr(extraction, "get_cache", Cache)

    signals.worker_ready.send(sender=None)
    tarefa = extraction.limpar_cache_de_versoes_antigas()
    tarefa.join(timeout=5)

    assert feito.wait(timeout=5)
    assert tarefa.daemon
    assert tarefa.name == "limpar-cache-extracao"


def test_task_runs_eagerly(monkeypatch) -> None:
    monkeypatch.setattr(extraction, "get_leitor", _leitor)
    monkeypatch.setattr(extraction, "get_cache", _cache)
    monkeypatch.setattr(extraction, "get_limiter", lambda: _limitador([]))

    resultado = extraction.extrair_bilhete_task.apply(kwargs=_argumentos()).get()

    assert resultado["degrau"] == "BARATO"
