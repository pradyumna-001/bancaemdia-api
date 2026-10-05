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
from math import ceil, isfinite
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
    max_idade_mv_segundos: float
    min_total_apostas: int
    passou: bool


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
    minimo_apostas = _inteiro_positivo("PAINEL_BENCH_MIN_BETS", 1)
    semaforo = asyncio.Semaphore(concorrencia)
    cabecalhos = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30, headers=cabecalhos) as cliente:

        async def uma() -> tuple[float, int, float | None, int | None]:
            async with semaforo:
                inicio = perf_counter()
                resposta = await cliente.get(url)
                latencia = (perf_counter() - inicio) * 1_000
                if resposta.status_code != 200:
                    return latencia, resposta.status_code, None, None
                corpo = resposta.json()
                idade = corpo.get("idade_mv_segundos")
                total = corpo.get("resumo", {}).get("total_apostas")
                if idade is None or not isinstance(total, int) or isinstance(total, bool):
                    raise RuntimeError("painel sem frescor ou total de apostas verificável")
                idade_numero = float(idade)
                if not isfinite(idade_numero) or idade_numero < 0:
                    raise RuntimeError("idade do painel inválida")
                return latencia, resposta.status_code, idade_numero, total

        await asyncio.gather(*(uma() for _ in range(aquecimento)))
        inicio_total = perf_counter()
        amostras = await asyncio.gather(*(uma() for _ in range(requisicoes)))
        duracao_total = perf_counter() - inicio_total

    latencias = [latencia for latencia, _, _, _ in amostras]
    status = Counter(codigo for _, codigo, _, _ in amostras)
    if set(status) != {200}:
        raise RuntimeError(f"benchmark recebeu respostas nao-200: {dict(status)}")
    idades = [idade for _, _, idade, _ in amostras if idade is not None]
    totais = [total for _, _, _, total in amostras if total is not None]
    p95_ms = round(_percentil(latencias, 95), 3)
    max_idade = round(max(idades), 3)
    min_apostas = min(totais)
    return Resultado(
        url=url,
        requisicoes=requisicoes,
        concorrencia=concorrencia,
        aquecimento=aquecimento,
        p50_ms=round(_percentil(latencias, 50), 3),
        p95_ms=p95_ms,
        p99_ms=round(_percentil(latencias, 99), 3),
        max_ms=round(max(latencias), 3),
        media_ms=round(sum(latencias) / len(latencias), 3),
        duracao_total_s=round(duracao_total, 3),
        respostas_por_status=dict(status),
        max_idade_mv_segundos=max_idade,
        min_total_apostas=min_apostas,
        passou=p95_ms < 500 and max_idade < 30 and min_apostas >= minimo_apostas,
    )


if __name__ == "__main__":
    resultado = asyncio.run(medir())
    sys.stdout.write(json.dumps(asdict(resultado), indent=2, sort_keys=True))
    sys.stdout.write("\n")
    raise SystemExit(0 if resultado.passou else 1)
