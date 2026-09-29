import asyncio
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bancaemdia.api.v1.metas import FusoEntrada, MetaEntrada, _progresso
from bancaemdia.models.meta_desempenho import MetaDesempenho


class Resultado:
    def mappings(self):
        return self

    def one(self):
        return {
            "total_apostas": 2,
            "pendentes": 0,
            "greens": 1,
            "reds": 1,
            "giro_centavos": 3_000,
            "base_roi_centavos": 3_000,
            "retorno_centavos": 4_000,
            "lucro_centavos": 1_000,
            "freebets": 0,
        }


class Sessao:
    async def execute(self, statement, parametros):
        await asyncio.sleep(0)
        assert parametros["usuario_id"] == 7
        assert "public.painel_analises_apostas" in str(statement)
        return Resultado()


def _meta(metrica: str = "lucro_centavos") -> MetaDesempenho:
    return MetaDesempenho(
        id=10,
        usuario_id=7,
        titulo="Meta",
        metrica=metrica,
        inicio=date(2026, 9, 1),
        fim=date(2026, 9, 30),
        alvo=Decimal(2_000),
        linha_base=Decimal(0),
        status="ativa",
    )


async def test_goal_progress_uses_canonical_profit_and_baseline() -> None:
    saida = await _progresso(Sessao(), _meta())
    assert saida.valor_atual == "1000.000000"
    assert saida.progresso == "0.500000"
    assert saida.alvo_atingido is False


async def test_goal_roi_is_derived_from_the_canonical_roi_denominator() -> None:
    meta = _meta("roi")
    meta.alvo = Decimal("0.50")
    saida = await _progresso(Sessao(), meta)
    assert saida.valor_atual == "0.333333"
    assert saida.progresso == "0.666667"


def test_goal_dates_and_timezone_are_validated() -> None:
    with pytest.raises(ValidationError):
        MetaEntrada(
            titulo="Inválida",
            metrica="roi",
            inicio=date(2026, 9, 30),
            fim=date(2026, 9, 1),
            alvo=Decimal(1),
        )
    with pytest.raises(ValidationError):
        FusoEntrada(fuso_horario="Horário da máquina")
    assert FusoEntrada(fuso_horario="America/Sao_Paulo").fuso_horario == "America/Sao_Paulo"
