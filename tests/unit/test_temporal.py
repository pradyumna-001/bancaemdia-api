from __future__ import annotations

from datetime import date

from bancaemdia.domain.financeiro import Aposta, Estado, resolver_retorno
from bancaemdia.domain.temporal import (
    VALOR_UNIDADE_PADRAO_CENTAVOS,
    ContaCasa,
    Movimento,
    TipoMovimento,
    Unidade,
    conta_casa_vigente,
    saldo,
    unidade_vigente,
)

UNIDADE = 10_000
BET365 = 1
BETANO = 2
BEFORE = date(2026, 7, 1)
PERIODOS = (
    Unidade(1_000, date(2026, 1, 1), date(2026, 7, 1)),
    Unidade(5_000, date(2026, 7, 1), None),
)


def _aposta(
    *,
    conta: int = BET365,
    odd: float = 2.00,
    stake: float = 1.0,
    estado: Estado = Estado.PENDENTE,
    freebet: bool = False,
    data: date | None = date(2026, 7, 15),
    criada_em: date | None = None,
    selecionada: bool = True,
    revisao_grave: bool = False,
) -> Aposta:
    return resolver_retorno(
        Aposta(
            stake_unidades=stake,
            valor_unidade_centavos=UNIDADE,
            odd=odd,
            estado=estado,
            freebet=freebet,
            conta_casa_id=conta,
            data_aposta=data,
            criada_em=criada_em,
            selecionada=selecionada,
            revisao_grave=revisao_grave,
        )
    )


def _deposito(valor: int, *, conta: int = BET365, em: date = BEFORE) -> Movimento:
    return Movimento(TipoMovimento.DEPOSITO, valor, conta, em)


def _saque(valor: int, *, conta: int = BET365, em: date = BEFORE) -> Movimento:
    return Movimento(TipoMovimento.SAQUE, -abs(valor), conta, em)


def test_unidade_vigente_picks_value_valid_on_date() -> None:
    assert unidade_vigente(PERIODOS, date(2026, 3, 10)) == 1_000
    assert unidade_vigente(PERIODOS, date(2026, 8, 10)) == 5_000


def test_unidade_vigente_de_inclusive_vigente_ate_exclusive() -> None:
    assert unidade_vigente(PERIODOS, date(2026, 6, 30)) == 1_000
    assert unidade_vigente(PERIODOS, date(2026, 7, 1)) == 5_000


def test_unidade_vigente_ate_exclusive_without_following_period() -> None:
    closed = [Unidade(1_000, date(2026, 1, 1), date(2026, 7, 1))]
    assert unidade_vigente(closed, date(2026, 6, 30)) == 1_000
    assert unidade_vigente(closed, date(2026, 7, 1)) == VALOR_UNIDADE_PADRAO_CENTAVOS


def test_unidade_vigente_default_when_uncovered() -> None:
    assert unidade_vigente(PERIODOS, date(2020, 1, 1)) == VALOR_UNIDADE_PADRAO_CENTAVOS
    assert unidade_vigente(PERIODOS, date(2020, 1, 1), padrao_centavos=777) == 777


def test_unidade_vigente_default_when_no_periods() -> None:
    assert unidade_vigente([], date(2026, 1, 1)) == VALOR_UNIDADE_PADRAO_CENTAVOS


def test_unidade_vigente_latest_vigente_de_wins_on_overlap() -> None:
    overlapping = [
        Unidade(1_000, date(2026, 1, 1), None),
        Unidade(5_000, date(2026, 7, 1), None),
    ]
    assert unidade_vigente(overlapping, date(2026, 8, 1)) == 5_000
    assert unidade_vigente(overlapping, date(2026, 2, 1)) == 1_000


def test_open_ended_conta_is_always_valid() -> None:
    conta = ContaCasa(casa_id=BET365)
    assert conta.valia_em(date(2020, 1, 1)) is True
    assert conta.valia_em(None) is True


def test_conta_desde_is_inclusive() -> None:
    conta = ContaCasa(casa_id=BET365, desde=date(2026, 7, 8))
    assert conta.valia_em(date(2026, 7, 7)) is False
    assert conta.valia_em(date(2026, 7, 8)) is True


def test_conta_ate_is_inclusive() -> None:
    conta = ContaCasa(casa_id=BET365, ate=date(2026, 7, 31))
    assert conta.valia_em(date(2026, 7, 31)) is True
    assert conta.valia_em(date(2026, 8, 1)) is False


def test_conta_no_date_never_excludes() -> None:
    conta = ContaCasa(casa_id=BET365, desde=date(2026, 7, 8), ate=date(2026, 7, 9))
    assert conta.valia_em(None) is True


def test_conta_casa_vigente_finds_the_matching_conta() -> None:
    contas = [
        ContaCasa(casa_id=BETANO, id=10),
        ContaCasa(casa_id=BET365, id=11, desde=date(2026, 7, 8)),
    ]
    assert conta_casa_vigente(contas, BET365, date(2026, 7, 10)) == contas[1]
    assert conta_casa_vigente(contas, BET365, date(2026, 7, 1)) is None
    assert conta_casa_vigente(contas, 99, date(2026, 7, 10)) is None


def test_conta_casa_vigente_skips_inactive_conta() -> None:
    contas = [ContaCasa(casa_id=BET365, ativa=False)]
    assert conta_casa_vigente(contas, BET365, None) is None
    assert conta_casa_vigente(contas, BET365, date(2026, 7, 8)) is None


def test_pendente_leaves_the_balance_but_is_not_lucro() -> None:
    result = saldo(BET365, [_aposta(stake=1.0)], [_deposito(50_000)])
    assert result.saldo_centavos == 40_000
    assert result.em_jogo_centavos == 10_000
    assert result.apostas_pendentes == 1
    assert result.lucro_centavos == 0


def test_settled_bet_becomes_lucro() -> None:
    result = saldo(BET365, [_aposta(estado=Estado.RED)], [_deposito(50_000)])
    assert result.lucro_centavos == -10_000
    assert result.em_jogo_centavos == 0
    assert result.saldo_centavos == 40_000


def test_saldo_is_deposit_minus_withdrawal_plus_net_result() -> None:
    result = saldo(BET365, [_aposta(estado=Estado.GREEN)], [_deposito(100_000), _saque(20_000)])
    assert result.depositado_centavos == 100_000
    assert result.sacado_centavos == 20_000
    assert result.saldo_centavos == 90_000


def test_saldo_is_independent_per_conta() -> None:
    apostas = [_aposta(conta=BETANO, estado=Estado.RED)]
    movimentos = [_deposito(50_000, conta=BET365), _deposito(30_000, conta=BETANO)]
    assert saldo(BET365, apostas, movimentos).saldo_centavos == 50_000
    assert saldo(BETANO, apostas, movimentos).saldo_centavos == 20_000


def test_movimento_without_conta_is_ignored() -> None:
    result = saldo(BET365, [], [Movimento(TipoMovimento.DEPOSITO, 5_000, None, BEFORE)])
    assert result.tem_caixa is False


def test_freebet_does_not_touch_the_balance() -> None:
    result = saldo(BET365, [_aposta(freebet=True, estado=Estado.GREEN)], [_deposito(50_000)])
    assert result.apostado_centavos == 0
    assert result.saldo_centavos == 60_000


def test_unselected_bet_is_ignored() -> None:
    result = saldo(BET365, [_aposta(selecionada=False)], [_deposito(50_000)])
    assert result.saldo_centavos == 50_000
    assert result.apostas_pendentes == 0


def test_bonus_counts_in_balance_but_not_as_deposit() -> None:
    movimentos = [_deposito(50_000), Movimento(TipoMovimento.BONUS, 5_000, BET365, BEFORE)]
    result = saldo(BET365, [], movimentos)
    assert result.depositado_centavos == 50_000
    assert result.bonus_centavos == 5_000
    assert result.saldo_centavos == 55_000


def test_transfer_and_adjustment_do_not_inflate_deposits() -> None:
    movimentos = [
        _deposito(50_000),
        Movimento(TipoMovimento.TRANSFERENCIA, -10_000, BET365, BEFORE),
        Movimento(TipoMovimento.AJUSTE, 1_500, BET365, BEFORE),
    ]
    result = saldo(BET365, [], movimentos)
    assert result.depositado_centavos == 50_000
    assert result.movido_centavos == -8_500
    assert result.saldo_centavos == 41_500


def test_grave_bet_moves_money_but_is_not_lucro() -> None:
    result = saldo(BET365, [_aposta(estado=Estado.GREEN, revisao_grave=True)], [_deposito(50_000)])
    assert result.saldo_centavos == 60_000
    assert result.lucro_centavos == 0


def test_no_ledger_means_unknown_saldo() -> None:
    result = saldo(BET365, [_aposta(estado=Estado.RED)], [])
    assert result.tem_caixa is False
    assert result.saldo_centavos is None
    assert result.deposito_faltante_centavos == 0
    assert result.lucro_centavos == -10_000


def test_bets_above_deposits_mean_unknown_saldo() -> None:
    result = saldo(BET365, [_aposta(stake=5.0, estado=Estado.RED)], [_deposito(10_000)])
    assert result.tem_caixa is True
    assert result.saldo_centavos is None
    assert result.deposito_faltante_centavos == 40_000


def test_saldo_that_closes_is_reported() -> None:
    result = saldo(BET365, [_aposta(estado=Estado.RED)], [_deposito(50_000)])
    assert result.saldo_centavos == 40_000
    assert result.deposito_faltante_centavos == 0


def test_lucro_can_still_be_negative() -> None:
    result = saldo(BET365, [_aposta(estado=Estado.RED)], [_deposito(100_000)])
    assert result.lucro_centavos == -10_000
    assert result.saldo_centavos == 90_000


def test_bet_before_first_movimento_is_out_of_the_balance() -> None:
    result = saldo(
        BET365,
        [_aposta(stake=5.0, estado=Estado.RED, data=date(2026, 7, 3))],
        [_deposito(50_000, em=date(2026, 7, 8))],
    )
    assert result.desde == date(2026, 7, 8)
    assert result.saldo_centavos == 50_000
    assert result.apostas_antes_do_caixa == 1


def test_lucro_counts_every_bet() -> None:
    result = saldo(
        BET365,
        [_aposta(stake=5.0, estado=Estado.RED, data=date(2026, 7, 3))],
        [_deposito(50_000, em=date(2026, 7, 8))],
    )
    assert result.lucro_centavos == -50_000


def test_backdated_deposit_brings_the_period_in() -> None:
    result = saldo(
        BET365,
        [_aposta(estado=Estado.RED, data=date(2026, 7, 3))],
        [_deposito(50_000, em=date(2026, 7, 1))],
    )
    assert result.apostas_antes_do_caixa == 0
    assert result.saldo_centavos == 40_000


def test_first_movimento_does_not_depend_on_list_order() -> None:
    movimentos = [_deposito(10_000, em=date(2026, 7, 20)), _deposito(40_000, em=date(2026, 7, 8))]
    assert saldo(BET365, [], movimentos).desde == date(2026, 7, 8)


def test_data_corte_excludes_later_movimentos_and_bets() -> None:
    apostas = [
        _aposta(estado=Estado.RED, data=date(2026, 7, 10)),
        _aposta(estado=Estado.RED, data=date(2026, 7, 20)),
    ]
    movimentos = [_deposito(50_000, em=date(2026, 7, 1)), _deposito(30_000, em=date(2026, 7, 15))]
    at_12 = saldo(BET365, apostas, movimentos, data_corte=date(2026, 7, 12))
    assert at_12.depositado_centavos == 50_000
    assert at_12.saldo_centavos == 40_000
    assert at_12.lucro_centavos == -10_000

    today = saldo(BET365, apostas, movimentos)
    assert today.saldo_centavos == 60_000
    assert today.lucro_centavos == -20_000


def test_data_corte_is_inclusive() -> None:
    result = saldo(
        BET365,
        [_aposta(estado=Estado.RED, data=date(2026, 7, 10))],
        [_deposito(50_000, em=date(2026, 7, 10))],
        data_corte=date(2026, 7, 10),
    )
    assert result.saldo_centavos == 40_000


def test_bet_without_data_aposta_falls_back_to_criada_em() -> None:
    result = saldo(
        BET365,
        [_aposta(estado=Estado.RED, data=None, criada_em=date(2026, 7, 3))],
        [_deposito(50_000, em=date(2026, 7, 8))],
    )
    assert result.apostas_antes_do_caixa == 1
    assert result.saldo_centavos == 50_000


def test_bet_without_any_date_always_enters() -> None:
    result = saldo(
        BET365,
        [_aposta(estado=Estado.RED, data=None)],
        [_deposito(50_000)],
        data_corte=date(2026, 1, 1),
    )
    assert result.apostado_no_periodo_centavos == 10_000
