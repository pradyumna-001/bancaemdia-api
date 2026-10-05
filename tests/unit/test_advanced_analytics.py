from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from bancaemdia.domain.analytics import analisar_apostas
from bancaemdia.domain.painel import (
    ContribuicaoEvolucao,
    PainelInvalidoError,
    PontoEvolucao,
    acumular_evolucao,
)


def aposta(
    *,
    estado: str = "GREEN",
    lucro: int = 500,
    stake: int = 1_000,
    face: int | None = None,
    odd: Decimal | None = Decimal("2.00"),
    instante: datetime = datetime(2026, 9, 28, 2, 30, tzinfo=UTC),
    esporte: int | None = 1,
    banca: int | None = 7,
    capital: int | None = 10_000,
) -> dict[str, object]:
    liquidada = estado not in {"PENDENTE", "ANULADA"}
    return {
        "total_apostas": 1,
        "pendentes": int(estado == "PENDENTE"),
        "greens": int(estado == "GREEN"),
        "reds": int(estado == "RED"),
        "giro_centavos": stake if liquidada else 0,
        "base_roi_centavos": (face if face is not None else stake) if liquidada else 0,
        "retorno_centavos": stake + lucro if liquidada else 0,
        "lucro_centavos": lucro if liquidada else 0,
        "freebets": int(stake == 0 and liquidada),
        "valor_face_centavos": face if face is not None else stake,
        "odd": odd,
        "estado": estado,
        "instante": instante,
        "esporte_id": esporte,
        "esporte_nome": "Futebol" if esporte else None,
        "banca_id": banca,
        "banca_nome": "Principal" if banca else None,
        "capital_banca_centavos": capital,
    }


def _reconciliar(saida: dict[str, object]) -> None:
    total = saida["total_filtrado"]
    for eixo in (
        "faixas_odds",
        "heatmap",
        "por_esporte",
        "quartis_stake",
        "por_banca_progressao",
    ):
        for campo in (
            "total_apostas",
            "pendentes",
            "greens",
            "reds",
            "giro_centavos",
            "base_roi_centavos",
            "lucro_centavos",
        ):
            assert sum(bucket[campo] for bucket in saida[eixo]) == total[campo]


def test_all_partitions_reconcile_with_missing_values_and_canonical_states() -> None:
    linhas = [
        aposta(),
        aposta(estado="RED", lucro=-1_000, odd=Decimal("1.8")),
        aposta(estado="PENDENTE", lucro=0, odd=None, esporte=None),
        aposta(estado="ANULADA", lucro=0, odd=None, banca=None, capital=None),
        aposta(estado="CASHOUT", lucro=-200, odd=Decimal("5")),
        aposta(stake=0, face=2_000, lucro=1_500, odd=Decimal("3.5")),
    ]
    saida = analisar_apostas(linhas, fuso_horario="America/Sao_Paulo")
    _reconciliar(saida)
    assert saida["total_filtrado"]["total_apostas"] == 6
    assert saida["total_filtrado"]["lucro_centavos"] == 800
    assert saida["profit_factor"] == "1.666667"
    assert next(b for b in saida["por_esporte"] if b["chave"] == "unknown")["total_apostas"] == 1
    assert next(b for b in saida["faixas_odds"] if b["chave"] == "unknown")["total_apostas"] == 2
    assert (
        next(b for b in saida["por_banca_progressao"] if b["chave"] == "unknown")["progressao"]
        is None
    )
    assert (
        sum(b["total_apostas"] for b in saida["quartis_stake"] if b["chave"] != "not_applicable")
        == 6
    )


def test_identical_face_values_collapse_and_no_loss_profit_factor_is_null() -> None:
    linhas = [aposta(face=1_000), aposta(face=1_000), aposta(stake=0, face=1_000)]
    saida = analisar_apostas(linhas, fuso_horario="UTC")
    _reconciliar(saida)
    assert [b["chave"] for b in saida["quartis_stake"]] == ["q1"]
    assert saida["profit_factor"] is None


def test_heatmap_uses_user_timezone_at_weekday_boundary() -> None:
    linha = aposta(instante=datetime(2026, 9, 28, 2, 30, tzinfo=UTC))
    brasil = analisar_apostas([linha], fuso_horario="America/Sao_Paulo")
    utc = analisar_apostas([linha], fuso_horario="UTC")
    assert next(b for b in brasil["heatmap"] if b["total_apostas"])["chave"] == "6:noite"
    assert next(b for b in utc["heatmap"] if b["total_apostas"])["chave"] == "0:madrugada"


def test_deposits_and_withdrawals_never_change_betting_profit() -> None:
    pontos = acumular_evolucao([
        ContribuicaoEvolucao(
            periodo_inicio=date(2026, 9, 28),
            contribuicao_centavos=500,
            banca_id=7,
            saldo_inicial_centavos=10_000,
            depositos_centavos=2_000,
            saques_centavos=300,
        ),
        ContribuicaoEvolucao(
            periodo_inicio=date(2026, 9, 29),
            contribuicao_centavos=-200,
            banca_id=7,
            saldo_inicial_centavos=10_000,
            depositos_centavos=1_000,
            saques_centavos=400,
        ),
    ])
    assert [
        (
            p.acumulado_centavos,
            p.depositos_acumulados_centavos,
            p.saques_acumulados_centavos,
            p.saldo_centavos,
        )
        for p in pontos
    ] == [
        (500, 2_000, 300, 12_200),
        (300, 3_000, 700, 12_600),
    ]


def test_evolution_rejects_a_non_reconciled_balance() -> None:
    with pytest.raises(PainelInvalidoError):
        PontoEvolucao(
            periodo_inicio=date(2026, 9, 28),
            contribuicao_centavos=500,
            acumulado_centavos=500,
            banca_id=7,
            banca_nome="Principal",
            saldo_centavos=10_500,
            saldo_inicial_centavos=10_000,
            depositos_acumulados_centavos=2_000,
            saques_acumulados_centavos=300,
        )
