"""Benchmark HTTP reproduzível do painel; não afirma SLO sem dados reais.

Exemplo::

    PAINEL_BENCH_TOKEN=... PAINEL_BENCH_URL=http://localhost:8000/api/v1/painel \
      python scripts/benchmark_painel.py

Configure uma base com cardinalidade representativa antes de usar o resultado no PR.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from math import ceil
from time import perf_counter

import httpx


@dataclass(frozen=True)
class Resultado:
    url: str
    requisicoes: int
    concorrencia: int
    aquecimento: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    media_ms: float
    duracao_total_s: float
    respostas_por_status: dict[int, int]


def _inteiro_positivo(nome: str, padrao: int) -> int:
    valor = int(os.environ.get(nome, str(padrao)))
    if valor <= 0:
        raise ValueError(f"{nome} precisa ser positivo")
    return valor


def _percentil(valores: list[float], percentil: int) -> float:
    ordenados = sorted(valores)
    indice = max(0, ceil(percentil / 100 * len(ordenados)) - 1)
    return ordenados[indice]


async def medir() -> Resultado:
    url = os.environ.get("PAINEL_BENCH_URL", "http://127.0.0.1:8000/api/v1/painel?periodo=30d")
    token = os.environ.get("PAINEL_BENCH_TOKEN")
    if not token:
        raise ValueError("PAINEL_BENCH_TOKEN e obrigatorio")
    requisicoes = _inteiro_positivo("PAINEL_BENCH_REQUESTS", 200)
    concorrencia = _inteiro_positivo("PAINEL_BENCH_CONCURRENCY", 10)
    aquecimento = _inteiro_positivo("PAINEL_BENCH_WARMUP", 20)
    semaforo = asyncio.Semaphore(concorrencia)
    cabecalhos = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30, headers=cabecalhos) as cliente:

        async def uma() -> tuple[float, int]:
            async with semaforo:
                inicio = perf_counter()
                resposta = await cliente.get(url)
                return (perf_counter() - inicio) * 1_000, resposta.status_code

        await asyncio.gather(*(uma() for _ in range(aquecimento)))
        inicio_total = perf_counter()
        amostras = await asyncio.gather(*(uma() for _ in range(requisicoes)))
        duracao_total = perf_counter() - inicio_total

    latencias = [latencia for latencia, _ in amostras]
    status = Counter(codigo for _, codigo in amostras)
    if set(status) != {200}:
        raise RuntimeError(f"benchmark recebeu respostas nao-200: {dict(status)}")
    return Resultado(
        url=url,
        requisicoes=requisicoes,
        concorrencia=concorrencia,
        aquecimento=aquecimento,
        p50_ms=round(_percentil(latencias, 50), 3),
        p95_ms=round(_percentil(latencias, 95), 3),
        p99_ms=round(_percentil(latencias, 99), 3),
        max_ms=round(max(latencias), 3),
        media_ms=round(sum(latencias) / len(latencias), 3),
        duracao_total_s=round(duracao_total, 3),
        respostas_por_status=dict(status),
    )


if __name__ == "__main__":
    sys.stdout.write(json.dumps(asdict(asyncio.run(medir())), indent=2, sort_keys=True))
    sys.stdout.write("\n")
