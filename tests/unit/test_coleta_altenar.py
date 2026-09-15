from __future__ import annotations

import pytest

from bancaemdia.coleta import altenar
from bancaemdia.coleta.leitura import ColetaInvalidaError


def _escolha(**campos):
    escolha = {
        "id": 4353878070,
        "status": 0,
        "price": 2.1,
        "name": "Menos de 27.5",
        "spec": '{"1": "27.5"}',
        "marketName": "Totais chutes",
        "marketTypeId": 15738,
        "childMarketTypeId": 0,
        "isLive": False,
        "isBetBuilder": False,
        "isBanker": False,
        "isVirtual": False,
        "dbId": 10,
        "eventId": 17362134,
        "eventName": "Fenerbahce SC vs. Olympique Lyonnais",
    }
    escolha.update(campos)
    return escolha


def _aposta(**campos):
    aposta = {
        "id": 5307388792,
        "status": 0,
        "type": 0,
        "combLength": 1,
        "linesCount": 1,
        "selectionsCount": 1,
        "currency": "BRL",
        "createdDate": "2026-08-18T16:35:40.99Z",
        "device": 1,
        "totalStake": 210,
        "finalStake": 210,
        "unitStake": 210,
        "totalOdds": 2.1,
        "totalWin": 441,
        "eventName": "Fenerbahce SC vs. Olympique Lyonnais",
        "bonus": 0,
        "bonusInsurance": 0,
        "bonusPart": 0,
        "bonusPartPercent": 0,
        "initBonusPart": 0,
        "cashOutValue": 0,
        "partialCashOut": 0,
        "partialCashouts": [],
        "isCancelAllowed": False,
        "selections": [_escolha()],
    }
    aposta.update(campos)
    return aposta


def test_top_level_fields_become_the_bet() -> None:
    coletada = altenar.ler(_aposta())

    assert coletada.casa == "altenar"
    assert coletada.identidade == "5307388792"
    assert coletada.odd == pytest.approx(2.1)
    assert coletada.tipo == "SIMPLES"


def test_money_comes_in_reais() -> None:
    coletada = altenar.ler(_aposta(status=1, totalStake=210, totalOdds=2.1, totalWin=441))

    assert coletada.estado == "GREEN"
    assert coletada.stake_centavos == 21000
    assert coletada.retorno_centavos == 44100


def test_total_win_of_a_running_bet_is_not_a_return() -> None:
    coletada = altenar.ler(_aposta(status=0, totalWin=441, openStake=210, remainingTotalWin=441))

    assert coletada.estado == "PENDENTE"
    assert coletada.retorno_centavos is None
    assert coletada.motivo_retencao is None


def test_lost_bet_zeroes_the_return() -> None:
    coletada = altenar.ler(_aposta(status=2, totalWin=0))

    assert (coletada.estado, coletada.retorno_centavos) == ("RED", 0)


def test_settled_bet_without_paid_value_is_held() -> None:
    aposta = _aposta(status=1)
    del aposta["totalWin"]

    coletada = altenar.ler(aposta)

    assert coletada.estado == "PENDENTE"
    assert coletada.retorno_centavos is None
    assert coletada.motivo_retencao == (
        "a casa liquidou esta aposta mas não mandou o valor pago — confira na casa"
    )


def test_freebet_is_held_with_the_bonus_fields() -> None:
    coletada = altenar.ler(
        _aposta(
            status=1,
            totalStake=10,
            totalOdds=2.025,
            totalWin=10.0,
            bonusMode=3,
            bonusPart=10,
            bonusPartPercent=100,
            initBonusPart=10,
            openPotWin=10.25,
        )
    )

    assert coletada.estado == "GREEN"
    assert "BÔNUS" in coletada.motivo_retencao
    assert "bonusPartPercent" in coletada.motivo_retencao


def test_partial_bonus_is_also_held() -> None:
    coletada = altenar.ler(_aposta(status=1, bonusPartPercent=40, bonusPart=4))

    assert "BÔNUS" in coletada.motivo_retencao


def test_unmeasured_cashout_is_held() -> None:
    coletada = altenar.ler(_aposta(status=1, cashOutValue=35.5))

    assert "cashout" in coletada.motivo_retencao


def test_unmeasured_status_is_held_with_the_house_number() -> None:
    coletada = altenar.ler(_aposta(status=7))

    assert coletada.estado == "PENDENTE"
    assert coletada.retorno_centavos is None
    assert "7" in coletada.motivo_retencao


def test_type_comes_from_event_id_not_comb_length() -> None:
    mesmo_jogo = [_escolha(id=1, eventId=99), _escolha(id=2, eventId=99)]
    jogos_diferentes = [_escolha(id=1, eventId=99), _escolha(id=2, eventId=100)]

    assert altenar.ler(_aposta(selections=mesmo_jogo, combLength=2)).tipo == "CRIAR_APOSTA"
    assert altenar.ler(_aposta(selections=jogos_diferentes, combLength=2)).tipo == "MULTIPLA"


def test_line_comes_from_json_inside_the_spec_text() -> None:
    coletada = altenar.ler(_aposta(selections=[_escolha(spec='{"1": "27.5"}')]))

    assert coletada.escolhas[0].linha == pytest.approx(27.5)


def test_unreadable_spec_does_not_drop_the_bet() -> None:
    coletada = altenar.ler(_aposta(selections=[_escolha(spec="isto nao e json")]))

    assert coletada.escolhas[0].linha is None
    assert coletada.stake_centavos == 21000


def test_market_and_game_come_from_the_selection() -> None:
    coletada = altenar.ler(_aposta())

    assert coletada.escolhas[0].mercado == "Totais chutes"
    assert coletada.evento == "Fenerbahce SC vs. Olympique Lyonnais"


def test_date_falls_back_to_placement_because_game_time_is_missing() -> None:
    coletada = altenar.ler(_aposta(createdDate="2026-08-18T16:35:40.99Z"))

    assert coletada.comeca_em is None
    assert coletada.data_aposta == "2026-08-18T13:35:40"


def test_currency_other_than_real_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="reais"):
        altenar.ler(_aposta(currency="EUR"))


def test_missing_stake_is_refused_and_never_becomes_zero() -> None:
    aposta = _aposta()
    del aposta["totalStake"]

    with pytest.raises(ColetaInvalidaError, match="valor apostado"):
        altenar.ler(aposta)
