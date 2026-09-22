from __future__ import annotations

import pytest

from bancaemdia.domain.financeiro import (
    Aposta,
    Estado,
    contar_pendentes,
    resolver_retorno,
    resumir,
)

UNIDADE = 10_000


def _aposta(
    *,
    odd: float | None = 1.90,
    stake: float = 1.0,
    estado: Estado = Estado.PENDENTE,
    freebet: bool = False,
    comissao: int = 0,
    selecionada: bool = True,
    revisao_grave: bool = False,
    revisao_motivo: str | None = None,
    retorno_centavos: int | None = None,
    retorno_informado: bool = False,
) -> Aposta:
    return resolver_retorno(
        Aposta(
            stake_unidades=stake,
            valor_unidade_centavos=UNIDADE,
            odd=odd,
            estado=estado,
            freebet=freebet,
            comissao_centavos=comissao,
            selecionada=selecionada,
            revisao_grave=revisao_grave,
            revisao_motivo=revisao_motivo,
            retorno_centavos=retorno_centavos,
            retorno_informado=retorno_informado,
        )
    )


@pytest.mark.parametrize(
    ("estado", "retorno", "lucro"),
    [
        (Estado.GREEN, 19_000, 9_000),
        (Estado.RED, 0, -10_000),
        (Estado.ANULADA, 10_000, 0),
        (Estado.MEIO_GREEN, 14_500, 4_500),
        (Estado.MEIO_RED, 5_000, -5_000),
    ],
)
def test_retorno_and_lucro_by_estado(estado: Estado, retorno: int, lucro: int) -> None:
    aposta = _aposta(estado=estado)
    assert aposta.retorno_centavos == retorno
    assert aposta.lucro_centavos == lucro


def test_pendente_has_no_retorno_or_lucro() -> None:
    aposta = _aposta()
    assert aposta.estado == Estado.PENDENTE
    assert aposta.retorno_centavos is None
    assert aposta.lucro_centavos is None


def test_no_odd_no_formula() -> None:
    aposta = _aposta(odd=None, estado=Estado.GREEN)
    assert aposta.retorno_centavos is None
    assert aposta.retorno_se_ganhar_centavos is None


def test_retorno_se_ganhar() -> None:
    assert _aposta().retorno_se_ganhar_centavos == 19_000


def test_cashout_uses_informed_value() -> None:
    aposta = _aposta(estado=Estado.CASHOUT, retorno_centavos=13_500, retorno_informado=True)
    assert aposta.retorno_centavos == 13_500
    assert aposta.lucro_centavos == 3_500


def test_cashout_without_value_has_no_formula() -> None:
    aposta = Aposta(
        stake_unidades=1.0, valor_unidade_centavos=UNIDADE, odd=1.90, estado=Estado.CASHOUT
    )
    assert aposta.retorno_calculado() is None
    assert resolver_retorno(aposta).retorno_centavos is None


def test_informed_value_survives_odd_change() -> None:
    aposta = _aposta(
        odd=3.00, estado=Estado.CASHOUT, retorno_centavos=13_500, retorno_informado=True
    )
    assert aposta.retorno_centavos == 13_500


def test_informed_value_wins_over_formula_in_any_estado() -> None:
    aposta = _aposta(estado=Estado.GREEN, retorno_centavos=12_000, retorno_informado=True)
    assert aposta.retorno_centavos == 12_000


def test_commission_deducted_on_green() -> None:
    assert _aposta(estado=Estado.GREEN, comissao=450).retorno_centavos == 19_000 - 450


def test_commission_deducted_on_meio_green() -> None:
    assert _aposta(estado=Estado.MEIO_GREEN, comissao=450).retorno_centavos == 14_500 - 450


def test_retorno_se_ganhar_is_net_of_commission() -> None:
    assert _aposta(comissao=450).retorno_se_ganhar_centavos == 19_000 - 450


def test_freebet_green_pays_profit_only() -> None:
    aposta = _aposta(freebet=True, estado=Estado.GREEN)
    assert aposta.stake_centavos == 0
    assert aposta.valor_aposta_centavos == 10_000
    assert aposta.retorno_centavos == 9_000
    assert aposta.lucro_centavos == 9_000


def test_freebet_red_loses_nothing() -> None:
    assert _aposta(freebet=True, estado=Estado.RED).lucro_centavos == 0


def test_regular_red_loses_stake() -> None:
    assert _aposta(freebet=False, estado=Estado.RED).lucro_centavos == -10_000


def test_freebet_anulada_returns_zero() -> None:
    assert _aposta(freebet=True, estado=Estado.ANULADA).retorno_centavos == 0


def test_freebet_half_states() -> None:
    assert _aposta(freebet=True, estado=Estado.MEIO_GREEN).retorno_centavos == 4_500
    assert _aposta(freebet=True, estado=Estado.MEIO_RED).retorno_centavos == 0


def test_stake_edited_after_result_recomputes_retorno() -> None:
    before = _aposta(estado=Estado.GREEN, stake=1.0)
    after = resolver_retorno(
        Aposta(
            stake_unidades=2.0,
            valor_unidade_centavos=UNIDADE,
            odd=1.90,
            estado=Estado.GREEN,
            retorno_centavos=before.retorno_centavos,
        )
    )
    assert before.retorno_centavos == 19_000
    assert after.retorno_centavos == 38_000


def test_resolver_retorno_returns_pendente_unchanged() -> None:
    aposta = Aposta(stake_unidades=1.0, valor_unidade_centavos=UNIDADE, odd=1.90)
    assert resolver_retorno(aposta) is aposta


def test_anulada_and_pendente_are_out_of_giro() -> None:
    assert _aposta(estado=Estado.ANULADA).conta_no_giro is False
    assert _aposta().conta_no_giro is False


def test_revisao_grave_is_out_of_giro() -> None:
    assert _aposta(estado=Estado.GREEN, revisao_grave=True).conta_no_giro is False


def test_settled_states_are_in_giro() -> None:
    for estado in (Estado.GREEN, Estado.RED, Estado.MEIO_GREEN, Estado.MEIO_RED):
        assert _aposta(estado=estado).conta_no_giro is True


def test_resumo_metrics() -> None:
    resumo = resumir([
        _aposta(odd=2.00, estado=Estado.GREEN),
        _aposta(odd=2.00, estado=Estado.RED),
    ])
    assert resumo.apostas == 2
    assert resumo.greens == 1
    assert resumo.reds == 1
    assert resumo.giro_centavos == 20_000
    assert resumo.retorno_centavos == 20_000
    assert resumo.lucro_centavos == 0
    assert resumo.taxa_de_acerto == pytest.approx(0.5)
    assert resumo.roi == pytest.approx(0.0)


def test_resumo_anulada_is_out_of_giro() -> None:
    resumo = resumir([
        _aposta(odd=2.00, estado=Estado.GREEN),
        _aposta(odd=2.00, estado=Estado.ANULADA),
    ])
    assert resumo.giro_centavos == 10_000
    assert resumo.roi == pytest.approx(1.0)


def test_resumo_pendente_is_counted_but_not_summed() -> None:
    resumo = resumir([_aposta()])
    assert resumo.pendentes == 1
    assert resumo.apostas == 1
    assert resumo.giro_centavos == 0


def test_resumo_ignores_unselected() -> None:
    resumo = resumir([_aposta(estado=Estado.GREEN, selecionada=False)])
    assert resumo.apostas == 0
    assert resumo.lucro_centavos == 0


def test_resumo_empty_has_no_division_by_zero() -> None:
    resumo = resumir([])
    assert resumo.roi == pytest.approx(0.0)
    assert resumo.roi_sem_bonus == pytest.approx(0.0)
    assert resumo.taxa_de_acerto == pytest.approx(0.0)


def test_roi_denominator_uses_freebet_face_value() -> None:
    resumo = resumir([
        _aposta(freebet=True, estado=Estado.GREEN),
        _aposta(freebet=True, estado=Estado.GREEN),
        _aposta(freebet=True, estado=Estado.GREEN),
        _aposta(estado=Estado.RED),
    ])
    assert resumo.freebets == 3
    assert resumo.lucro_centavos == 3 * 9_000 - 10_000
    assert resumo.giro_centavos == 10_000
    assert resumo.base_roi_centavos == 40_000
    assert resumo.roi == pytest.approx(17_000 / 40_000)
    assert resumo.giro_proprio_centavos == 10_000
    assert resumo.lucro_proprio_centavos == -10_000
    assert resumo.roi_sem_bonus == pytest.approx(1.7)


def test_revisao_grave_is_out_of_every_number() -> None:
    resumo = resumir([
        _aposta(estado=Estado.GREEN, revisao_grave=True, revisao_motivo="odd"),
        _aposta(estado=Estado.RED),
    ])
    assert resumo.em_revisao_grave == 1
    assert resumo.apostas == 1
    assert resumo.lucro_centavos == -10_000


def test_revisao_leve_counts_and_is_reported() -> None:
    resumo = resumir([_aposta(estado=Estado.GREEN, revisao_motivo="texto")])
    assert resumo.em_revisao_leve == 1
    assert resumo.lucro_centavos == 9_000
    assert resumo.lucro_a_confirmar_centavos == 9_000


def test_contar_pendentes_splits_by_confidence() -> None:
    contagem = contar_pendentes([
        _aposta(),
        _aposta(revisao_grave=True),
        _aposta(estado=Estado.GREEN),
    ])
    assert contagem.confiaveis == 1
    assert contagem.suspeitas == 1
    assert contagem.total == 2
    assert contagem.stake_confiavel_centavos == 10_000
    assert contagem.stake_suspeita_centavos == 10_000
    assert contagem.stake_total_centavos == 20_000


def test_contar_pendentes_skips_unselected_by_default() -> None:
    apostas = [_aposta(selecionada=False), _aposta()]
    assert contar_pendentes(apostas).total == 1
    assert contar_pendentes(apostas, so_selecionadas=False).total == 2


def test_resumo_and_contagem_agree() -> None:
    apostas = [_aposta(), _aposta(revisao_grave=True, revisao_motivo="x")]
    resumo = resumir(apostas)
    contagem = contar_pendentes(apostas)
    assert resumo.pendentes == contagem.confiaveis == 1
    assert resumo.pendentes_suspeitas == contagem.suspeitas == 1
