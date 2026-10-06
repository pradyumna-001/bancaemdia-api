from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from bancaemdia.domain.painel import (
    ContribuicaoEvolucao,
    FiltrosPainel,
    GranularidadePainel,
    MetricasPainel,
    PainelInvalidoError,
    PeriodoPainel,
    PontoPeriodo,
    SaldoPainel,
    acumular_evolucao,
    basis_points,
    calcular_frescor,
    formatar_decimal,
    janela_do_periodo,
    metricas_para_graficos,
)


def test_period_window_uses_the_brazilian_day_and_an_exclusive_end() -> None:
    # Ainda e 21/09 em Sao Paulo, apesar de ja ser 22/09 em UTC.
    agora = datetime(2026, 9, 22, 2, 30, tzinfo=UTC)

    janela = janela_do_periodo(" 7D ", agora)

    assert janela.inicio == date(2026, 9, 15)
    assert janela.fim == date(2026, 9, 22)
    assert janela.granularidade == GranularidadePainel.DIA


@pytest.mark.parametrize(
    ("periodo", "inicio", "granularidade"),
    [
        ("30d", date(2026, 8, 23), GranularidadePainel.DIA),
        ("90d", date(2026, 6, 24), GranularidadePainel.SEMANA),
        ("1y", date(2025, 9, 21), GranularidadePainel.MES),
        ("all", None, GranularidadePainel.MES),
    ],
)
def test_each_period_has_one_stable_window(
    periodo: str, inicio: date | None, granularidade: GranularidadePainel
) -> None:
    janela = janela_do_periodo(periodo, datetime(2026, 9, 21, 12))

    assert (janela.inicio, janela.fim, janela.granularidade) == (
        inicio,
        date(2026, 9, 22),
        granularidade,
    )


def test_one_calendar_year_handles_february_29() -> None:
    janela = janela_do_periodo("1y", datetime(2028, 2, 29, 12))

    assert (janela.inicio, janela.fim) == (date(2027, 2, 28), date(2028, 3, 1))


@pytest.mark.parametrize(
    ("periodo", "casa_id", "tipster_id", "mercado_id"),
    [
        ("ontem", None, None, None),
        ("30d", 0, None, None),
        ("30d", None, -1, None),
        ("30d", None, None, True),
    ],
)
def test_invalid_filters_are_rejected(
    periodo: str, casa_id: int | None, tipster_id: int | None, mercado_id: int | None
) -> None:
    with pytest.raises(PainelInvalidoError):
        FiltrosPainel.criar(
            periodo,
            casa_id=casa_id,
            tipster_id=tipster_id,
            mercado_id=mercado_id,
            agora=datetime(2026, 9, 21, 12),
        )


def test_filters_keep_all_dimensions_together() -> None:
    filtros = FiltrosPainel.criar(
        PeriodoPainel.NOVENTA_DIAS,
        casa_id=11,
        tipster_id=22,
        mercado_id=33,
        agora=datetime(2026, 9, 21, 12),
    )

    assert (filtros.casa_id, filtros.tipster_id, filtros.mercado_id) == (11, 22, 33)
    assert filtros.janela.granularidade == GranularidadePainel.SEMANA


def test_metrics_are_derived_from_exact_integer_amounts() -> None:
    metricas = MetricasPainel.de_linha({
        "total_apostas": Decimal("4"),
        "pendentes": 1,
        "greens": 1,
        "reds": 2,
        "giro_centavos": 30_000,
        # Inclui o valor facial de uma freebet; o dominio nao tenta refaze-lo.
        "base_roi_centavos": 40_000,
        "retorno_centavos": 47_000,
        "lucro_centavos": 17_000,
        "freebets": 1,
        # Mesmo que a MV exponha razoes, a fonte da verdade sao numerador/denominador.
        "roi": Decimal("999"),
        "win_rate": Decimal("999"),
    })

    assert metricas.roi == Decimal(17_000) / Decimal(40_000)
    assert metricas.win_rate == Decimal(1) / Decimal(3)
    assert metricas.roi_basis_points == 4_250
    assert metricas.win_rate_basis_points == 3_333


def test_empty_metrics_do_not_divide_by_zero() -> None:
    metricas = MetricasPainel()

    assert metricas.roi == Decimal(0)
    assert metricas.win_rate == Decimal(0)


def test_summing_metrics_preserves_cents_above_float_precision() -> None:
    grande = 9_007_199_254_740_993

    total = MetricasPainel.somar([
        MetricasPainel(
            total_apostas=1,
            giro_centavos=grande,
            base_roi_centavos=grande,
            retorno_centavos=grande + 1,
            lucro_centavos=1,
        ),
        MetricasPainel(total_apostas=1, lucro_centavos=-2),
    ])

    assert total.giro_centavos == grande
    assert total.retorno_centavos == grande + 1
    assert total.lucro_centavos == -1


@pytest.mark.parametrize(
    "linha",
    [
        {"lucro_centavos": 1.5},
        {"total_apostas": True},
        {"giro_centavos": Decimal("1.2")},
        {"pendentes": -1},
    ],
)
def test_metrics_refuse_inexact_or_impossible_values(linha: dict[str, object]) -> None:
    with pytest.raises(PainelInvalidoError):
        MetricasPainel.de_linha(linha)


def test_decimal_formatting_and_basis_points_never_need_float() -> None:
    um_terco = Decimal(1) / Decimal(3)

    assert formatar_decimal(um_terco) == "0.333333"
    assert basis_points(um_terco) == 3_333


def test_evolution_groups_equal_days_sorts_and_accumulates_signed_contributions() -> None:
    evolucao = acumular_evolucao([
        ContribuicaoEvolucao(date(2026, 9, 3), -500, 7, "Principal", 10_000),
        ContribuicaoEvolucao(date(2026, 9, 1), 1_000, 7, "Principal", 10_000),
        ContribuicaoEvolucao(date(2026, 9, 3), 200, 7, "Principal", 10_000),
    ])

    assert [ponto.periodo_inicio for ponto in evolucao] == [
        date(2026, 9, 1),
        date(2026, 9, 3),
    ]
    assert [(p.contribuicao_centavos, p.acumulado_centavos) for p in evolucao] == [
        (1_000, 1_000),
        (-300, 700),
    ]
    assert [ponto.saldo_centavos for ponto in evolucao] == [11_000, 10_700]


def test_evolution_never_calls_unknown_initial_money_a_balance() -> None:
    evolucao = acumular_evolucao([
        ContribuicaoEvolucao(date(2026, 9, 1), 2_000, 8, "Sem inicial", None),
    ])

    assert evolucao[0].acumulado_centavos == 2_000
    assert evolucao[0].saldo_centavos is None


def test_evolution_accumulates_each_bank_independently() -> None:
    evolucao = acumular_evolucao([
        ContribuicaoEvolucao(date(2026, 9, 1), 100, 2, "B", 2_000),
        ContribuicaoEvolucao(date(2026, 9, 1), 50, 1, "A", 1_000),
        ContribuicaoEvolucao(date(2026, 9, 2), -20, 1, "A", 1_000),
    ])

    assert [(p.banca_id, p.acumulado_centavos, p.saldo_centavos) for p in evolucao] == [
        (1, 50, 1_050),
        (1, 30, 1_030),
        (2, 100, 2_100),
    ]


def test_evolution_carries_the_filtered_history_before_the_window() -> None:
    evolucao = acumular_evolucao([
        ContribuicaoEvolucao(
            date(2026, 9, 1),
            100,
            1,
            "Principal",
            1_000,
            acumulado_anterior_centavos=500,
        ),
        ContribuicaoEvolucao(
            date(2026, 9, 2),
            -50,
            1,
            "Principal",
            1_000,
            acumulado_anterior_centavos=500,
        ),
    ])

    assert [(p.acumulado_centavos, p.saldo_centavos) for p in evolucao] == [
        (600, 1_600),
        (550, 1_550),
    ]


def test_known_account_balance_is_distinct_from_bank_evolution() -> None:
    saldo = SaldoPainel(
        saldo_total_centavos=75_000,
        saldo_conhecido_centavos=75_000,
        contas_saldo_desconhecido=0,
    )

    assert saldo.escopo == "contas_casa_all_time"
    assert saldo.saldo_total_centavos == saldo.saldo_conhecido_centavos


def test_an_unknown_house_account_makes_only_the_total_unavailable() -> None:
    saldo = SaldoPainel(
        saldo_total_centavos=None,
        saldo_conhecido_centavos=75_000,
        contas_saldo_desconhecido=1,
    )

    assert saldo.saldo_total_centavos is None
    assert saldo.saldo_conhecido_centavos == 75_000


def test_no_house_account_has_no_invented_total() -> None:
    saldo = SaldoPainel.de_linha({
        "saldo_centavos": None,
        "saldo_conhecido_centavos": 0,
        "contas_saldo_desconhecido": 0,
    })

    assert saldo.saldo_total_centavos is None
    assert saldo.saldo_conhecido_centavos == 0
    assert saldo.escopo == "contas_casa_all_time"


@pytest.mark.parametrize(
    "saldo",
    [
        (80_000, 75_000, 0),
        (75_000, 75_000, 1),
        (None, 75_000, 0),
    ],
)
def test_balance_contract_refuses_invented_totals(saldo: tuple[int | None, int, int]) -> None:
    with pytest.raises(PainelInvalidoError):
        SaldoPainel(
            saldo_total_centavos=saldo[0],
            saldo_conhecido_centavos=saldo[1],
            contas_saldo_desconhecido=saldo[2],
        )


def test_freshness_keeps_mv_age_response_time_and_replica_lag_separate() -> None:
    atualizado = datetime(2026, 9, 21, 15, 0, 0, 250_000, tzinfo=UTC)
    respondido = datetime(2026, 9, 21, 15, 0, 12, 750_000, tzinfo=UTC)

    frescor = calcular_frescor(
        atualizado,
        Decimal("1.125"),
        respondido_em=respondido,
    )

    assert frescor.atualizado_em == atualizado
    assert frescor.respondido_em == respondido
    assert frescor.idade_mv_segundos == Decimal("12.5")
    assert frescor.replica_atraso_segundos == Decimal("1.125")


def test_primary_or_environment_without_replica_keeps_lag_unknown() -> None:
    respondido = datetime(2026, 9, 21, 15, tzinfo=UTC)

    frescor = calcular_frescor(None, None, respondido_em=respondido)

    assert frescor.atualizado_em is None
    assert frescor.idade_mv_segundos is None
    assert frescor.replica_atraso_segundos is None


def test_future_clocks_are_clamped_but_not_relabelled_as_refresh() -> None:
    respondido = datetime(2026, 9, 21, 15, tzinfo=UTC)
    atualizado = datetime(2026, 9, 21, 15, 0, 1, tzinfo=UTC)

    frescor = calcular_frescor(atualizado, Decimal("-0.5"), respondido_em=respondido)

    assert frescor.atualizado_em == atualizado
    assert frescor.idade_mv_segundos == 0
    assert frescor.replica_atraso_segundos == 0


def test_chart_contract_is_ordered_and_contains_only_integer_data() -> None:
    pontos = [
        PontoPeriodo(
            date(2026, 9, 2),
            GranularidadePainel.DIA,
            MetricasPainel(
                total_apostas=2,
                greens=1,
                reds=1,
                giro_centavos=20_000,
                base_roi_centavos=20_000,
                retorno_centavos=22_000,
                lucro_centavos=2_000,
            ),
        ),
        PontoPeriodo(
            date(2026, 9, 1),
            GranularidadePainel.DIA,
            MetricasPainel(total_apostas=1, pendentes=1),
        ),
    ]

    graficos = metricas_para_graficos(pontos)

    assert graficos.labels == ("2026-09-01", "2026-09-02")
    assert {serie.chave for serie in graficos.datasets} == {
        "lucro_centavos",
        "giro_centavos",
        "total_apostas",
        "roi_basis_points",
        "win_rate_basis_points",
    }
    assert all(isinstance(valor, int) for serie in graficos.datasets for valor in serie.data)


def test_chart_contract_rejects_mixed_granularities() -> None:
    with pytest.raises(PainelInvalidoError):
        metricas_para_graficos([
            PontoPeriodo(date(2026, 9, 1), GranularidadePainel.DIA, MetricasPainel()),
            PontoPeriodo(date(2026, 9, 2), GranularidadePainel.SEMANA, MetricasPainel()),
        ])


def test_empty_chart_keeps_the_period_granularity() -> None:
    graficos = metricas_para_graficos(
        [],
        granularidade_esperada=GranularidadePainel.MES,
    )

    assert graficos.granularidade == GranularidadePainel.MES
    assert graficos.labels == ()
    assert all(serie.data == () for serie in graficos.datasets)
