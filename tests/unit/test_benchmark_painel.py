from __future__ import annotations

import httpx
import pytest

from scripts import benchmark_painel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("idade", "apostas", "esperado"),
    [(29.5, 16_000, True), (30.0, 16_000, False), (10.0, 15_999, False)],
)
async def test_benchmark_checks_latency_freshness_and_dataset(
    monkeypatch: pytest.MonkeyPatch, idade: float, apostas: int, esperado: bool
) -> None:
    monkeypatch.setenv("PAINEL_BENCH_TOKEN", "test-token")
    monkeypatch.setenv("PAINEL_BENCH_REQUESTS", "2")
    monkeypatch.setenv("PAINEL_BENCH_WARMUP", "1")
    monkeypatch.setenv("PAINEL_BENCH_CONCURRENCY", "1")
    monkeypatch.setenv("PAINEL_BENCH_MIN_BETS", "16000")
    client_type = httpx.AsyncClient

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200, json={"idade_mv_segundos": str(idade), "resumo": {"total_apostas": apostas}}
        )

    def cliente(*, timeout: int, headers: dict[str, str]) -> httpx.AsyncClient:
        return client_type(
            transport=httpx.MockTransport(responder), timeout=timeout, headers=headers
        )

    monkeypatch.setattr(benchmark_painel.httpx, "AsyncClient", cliente)
    resultado = await benchmark_painel.medir()
    assert resultado.passou is esperado
    assert resultado.max_idade_mv_segundos == idade
    assert resultado.min_total_apostas == apostas


@pytest.mark.asyncio
async def test_benchmark_rejects_missing_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAINEL_BENCH_TOKEN", "test-token")
    monkeypatch.setenv("PAINEL_BENCH_REQUESTS", "1")
    monkeypatch.setenv("PAINEL_BENCH_WARMUP", "1")
    client_type = httpx.AsyncClient

    def cliente(*, timeout: int, headers: dict[str, str]) -> httpx.AsyncClient:
        return client_type(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"resumo": {"total_apostas": 16_000}})
            ),
            timeout=timeout,
            headers=headers,
        )

    monkeypatch.setattr(benchmark_painel.httpx, "AsyncClient", cliente)
    with pytest.raises(RuntimeError, match="sem frescor"):
        await benchmark_painel.medir()
