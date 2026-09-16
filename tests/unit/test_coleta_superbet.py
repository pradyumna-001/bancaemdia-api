from __future__ import annotations

import pytest

from bancaemdia.coleta import superbet
from bancaemdia.coleta.leitura import ColetaInvalidaError


def _evento(
    evento_id="13358231",
    odd=2.57,
    escolha="Ambas as equipes marcam; Mais de 3.5 gols na partida",
    mercado="Super Odds",
    linha=None,
    estado="LOST",
    **campos,
):
    evento = {
        "categoryId": "127",
        "coefficient": odd,
        "date": "2026-08-02T16:00:00.000Z",
        "eventComponents": [],
        "eventId": evento_id,
        "externalIds": {"betradar": "71780786"},
        "inputCode": {"event": "6107", "market": ""},
        "market": {"marketId": "234615", "name": mercado},
        "marketing": "",
        "name": ["PSV Eindhoven", "AZ Alkmaar"],
        "odd": {
            "coefficient": odd,
            "isFix": None,
            "name": escolha,
            "oddId": "9489",
            "oddStatus": estado,
            "oddUuid": "a729082e-021d-5f56-983a-3910adc49097",
            "resultReason": "UNSPECIFIED",
            "sourceType": 202,
            "specialValue": linha,
            "systemGroupName": None,
            "voidFactor": 0,
        },
        "repriceStatus": "UNSPECIFIED",
        "sportId": "5",
        "status": estado.lower(),
        "tournamentId": "1532",
        "type": "static",
    }
    evento.update(campos)
    return evento


def _ticket(
    eventos=None,
    stake=140,
    odd=2.57,
    status="lost",
    payoff=0,
    total_winnings=0,
    recebida="2026-08-02T15:35:38.721Z",
    **campos,
):
    bruto = {
        "bonus": None,
        "bonuses": {},
        "bonusesEligibility": {"LUCKY_LOSER": False, "PROFIT_BOOST": False, "SUPER_BONUS": False},
        "codeTicketOrigin": "mobile",
        "coefficient": odd,
        "dateLastModified": "2026-08-02T17:59:01.424Z",
        "datePayoff": None,
        "dateReceived": recebida,
        "deviceIdentifier": "25c56629-3c3d-4a54-a928-450394ead33b",
        "events": eventos if eventos is not None else [_evento()],
        "initialCoefficient": odd,
        "isTemplate": False,
        "isTest": False,
        "lotto": None,
        "payment": {
            "bonusAmount": None,
            "bonusType": "no_bonus",
            "branchId": "898",
            "handlingFee": 0,
            "stake": stake,
            "tax": 0,
            "terminalId": "P",
            "total": stake,
        },
        "source": "online",
        "status": status,
        "system": None,
        "ticketId": "898P-7YI9MN",
        "type": "sports",
        "win": {
            "branchId": None,
            "estimated": round(stake * odd, 2),
            "isCashedOut": False,
            "isLuckyLoser": False,
            "minPotential": round(stake * odd, 2),
            "payoff": payoff,
            "perHitStatistics": [],
            "potentialPayoff": round(stake * odd, 2),
            "potentialTax": 0,
            "returnBonusStake": False,
            "tax": 0,
            "taxBrackets": [],
            "totalWinnings": total_winnings,
        },
    }
    bruto.update(campos)
    return bruto


def test_top_level_fields_become_the_bet() -> None:
    lida = superbet.ler(_ticket())

    assert lida.casa == "superbet"
    assert lida.identidade == "898P-7YI9MN"
    assert lida.odd == pytest.approx(2.57)
    assert lida.tipo == "SIMPLES"
    assert lida.observacao is None


def test_stake_comes_in_reais_not_cents() -> None:
    assert superbet.ler(_ticket(stake=140, odd=2.57)).stake_centavos == 14000


def test_game_date_in_utc_becomes_local_time() -> None:
    lida = superbet.ler(_ticket(eventos=[_evento(date="2026-08-02T15:35:38.721Z")]))

    assert lida.comeca_em == "2026-08-02T12:35:38"
    assert lida.data_aposta == "2026-08-02T12:35:38"


def test_placement_date_converts_too_when_there_is_no_game_date() -> None:
    evento = _evento()
    del evento["date"]

    lida = superbet.ler(_ticket(eventos=[evento], recebida="2026-08-02T15:35:38.721Z"))

    assert lida.comeca_em is None
    assert lida.data_aposta == "2026-08-02T12:35:38"


def test_game_at_9pm_local_does_not_jump_to_the_next_day() -> None:
    lida = superbet.ler(_ticket(eventos=[_evento(date="2026-08-03T00:30:00.000Z")]))

    assert lida.data_aposta == "2026-08-02T21:30:00"


def test_bet_counts_on_the_game_day_not_the_placement_day() -> None:
    lida = superbet.ler(
        _ticket(
            eventos=[_evento(date="2026-08-05T22:00:00.000Z")],
            recebida="2026-08-02T15:35:38.721Z",
        )
    )

    assert lida.data_aposta == "2026-08-05T19:00:00"


def test_date_without_timezone_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="sem fuso"):
        superbet.ler(_ticket(recebida="2026-08-02T15:35:38"))


def test_both_teams_come_in_an_array_and_become_the_matchup() -> None:
    assert superbet.ler(_ticket()).evento == "PSV Eindhoven x AZ Alkmaar"


def test_selection_is_read_from_the_single_odd_object() -> None:
    lida = superbet.ler(_ticket(eventos=[_evento(linha=3.5)]))

    (escolha,) = lida.escolhas
    assert escolha.descricao == "Ambas as equipes marcam; Mais de 3.5 gols na partida"
    assert escolha.mercado == "Super Odds"
    assert escolha.mercado_id == 234615
    assert escolha.linha == pytest.approx(3.5)
    assert escolha.resultado == "LOST"
    assert escolha.evento_id == "13358231"
    assert lida.mercado_bruto == "Super Odds"


def test_bet_odd_is_the_top_one_and_nothing_is_multiplied() -> None:
    eventos = [_evento(evento_id="777", odd=2.0), _evento(evento_id="888", odd=3.0)]

    lida = superbet.ler(_ticket(eventos=eventos, odd=5.5))

    assert lida.odd == pytest.approx(5.5)
    assert [e.odd for e in lida.escolhas] == [pytest.approx(2.0), pytest.approx(3.0)]


def test_boosted_odd_is_the_one_the_house_would_pay() -> None:
    eventos = [_evento(odd=2.57, priceBoost={"boostLevel": "super", "originalPrice": 2.12})]

    assert superbet.ler(_ticket(eventos=eventos, odd=2.57)).odd == pytest.approx(2.57)


def test_lost_bet_returns_zero() -> None:
    lida = superbet.ler(_ticket(status="lost"))

    assert (lida.estado, lida.retorno_centavos, lida.motivo_retencao) == ("RED", 0, None)


def test_open_bet_is_pending_without_reason() -> None:
    lida = superbet.ler(_ticket(status="active"))

    assert (lida.estado, lida.retorno_centavos, lida.motivo_retencao) == ("PENDENTE", None, None)


def test_won_bet_carries_what_the_house_paid() -> None:
    lida = superbet.ler(_ticket(status="win", payoff=240.0, total_winnings=240.0))

    assert (lida.estado, lida.retorno_centavos) == ("GREEN", 24000)
    assert lida.motivo_retencao is None


def test_won_spelled_won_is_not_a_status_the_house_sends() -> None:
    lida = superbet.ler(_ticket(status="won", payoff=240.0))

    assert lida.estado == "PENDENTE"
    assert lida.motivo_retencao == (
        "a casa marcou esta aposta como 'won', que eu ainda não sei ler — confira e me diga o que"
        " ela foi"
    )


def test_unknown_status_is_held_with_the_word_the_house_sent() -> None:
    lida = superbet.ler(_ticket(status="cancelado_pela_casa"))

    assert lida.estado == "PENDENTE"
    assert "cancelado_pela_casa" in (lida.motivo_retencao or "")


def test_diverging_return_numbers_keep_the_payoff_and_hold_the_bet() -> None:
    lida = superbet.ler(_ticket(status="win", payoff=300.0, total_winnings=359.8))

    assert (lida.estado, lida.retorno_centavos) == ("GREEN", 30000)
    assert lida.motivo_retencao == (
        "a casa mandou dois números de retorno diferentes (pago 300.0, ganho 359.8) — confira na casa"
    )


def test_bonus_that_explains_the_difference_is_named_and_still_holds() -> None:
    lida = superbet.ler(
        _ticket(
            status="win",
            stake=100,
            odd=1.9,
            payoff=217.0,
            total_winnings=190.0,
            bonuses={"PROFIT_BOOST": {"percentage": 30, "value": 27}},
        )
    )

    assert (lida.estado, lida.retorno_centavos) == ("GREEN", 21700)
    assert lida.motivo_retencao == (
        "a casa mandou dois números de retorno diferentes (pago 217.0, ganho 190.0), e a diferença"
        " bate com o bônus PROFIT_BOOST de 27 que a casa declarou nesta aposta — confira na casa"
    )


def test_bonus_that_does_not_match_the_difference_is_not_named() -> None:
    lida = superbet.ler(
        _ticket(
            status="win",
            payoff=300.0,
            total_winnings=359.8,
            bonuses={"PROFIT_BOOST": {"percentage": 30, "value": 27}},
        )
    )

    assert lida.retorno_centavos == 30000
    assert "dois números de retorno" in (lida.motivo_retencao or "")
    assert "PROFIT_BOOST" not in (lida.motivo_retencao or "")


def test_system_bet_is_held_with_reason() -> None:
    lida = superbet.ler(_ticket(system={"name": "2/3"}))

    assert "SISTEMA" in (lida.motivo_retencao or "")


def test_no_bonus_does_not_hold_and_unknown_bonus_does() -> None:
    assert superbet.ler(_ticket()).motivo_retencao is None

    com_bonus = _ticket()
    com_bonus["payment"]["bonusType"] = "free_bet"

    assert superbet.ler(com_bonus).motivo_retencao == (
        "a casa marcou um bônus nesta aposta (bonusType='free_bet') e eu ainda não sei o que ele"
        " significa — confira se ela saiu do seu bolso"
    )


def test_missing_ticket_id_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="identificador"):
        superbet.ler(_ticket(ticketId=""))


def test_missing_stake_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="sem valor apostado"):
        superbet.ler(_ticket(stake=0))


def test_missing_odd_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="odd"):
        superbet.ler(_ticket(odd=0))


def test_non_object_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="não veio como objeto"):
        superbet.ler([_ticket()])


def test_cashout_without_payoff_is_held() -> None:
    lida = superbet.ler(_ticket(status="win", win={"isCashedOut": True}))

    assert (lida.estado, lida.retorno_centavos) == ("CASHOUT", None)
    assert "não mandou o valor" in (lida.motivo_retencao or "")


def test_cashout_wins_over_status() -> None:
    lida = superbet.ler(
        _ticket(
            status="win",
            stake=150,
            win={"payoff": 150.0, "totalWinnings": 150.0, "isCashedOut": True, "estimated": 292.5},
        )
    )

    assert (lida.estado, lida.retorno_centavos) == ("CASHOUT", 15000)


def test_refund_without_returned_amount_is_held() -> None:
    bruto = _ticket(status="refund", stake=100, odd=1, initialCoefficient=1.77)
    del bruto["win"]["payoff"]

    lida = superbet.ler(bruto)

    assert (lida.estado, lida.retorno_centavos) == ("ANULADA", None)
    assert "não mandou o valor" in (lida.motivo_retencao or "")


def test_refund_returns_the_stake_and_is_not_a_loss() -> None:
    lida = superbet.ler(
        _ticket(
            status="refund",
            stake=100,
            odd=1,
            payoff=100.0,
            total_winnings=100.0,
            initialCoefficient=1.77,
        )
    )

    assert (lida.estado, lida.retorno_centavos, lida.stake_centavos) == ("ANULADA", 10000, 10000)
    assert lida.motivo_retencao is None


def test_refund_odd_is_the_one_the_bet_had() -> None:
    lida = superbet.ler(
        _ticket(status="refund", stake=100, odd=1, payoff=100.0, initialCoefficient=1.77)
    )

    assert lida.odd == pytest.approx(1.77)


def test_outside_refund_the_odd_stays_the_current_one() -> None:
    lida = superbet.ler(
        _ticket(status="win", odd=2.57, payoff=359.8, total_winnings=359.8, initialCoefficient=2.12)
    )

    assert lida.odd == pytest.approx(2.57)


def test_single_selection_is_simples() -> None:
    assert superbet.ler(_ticket()).tipo == "SIMPLES"


def test_two_selections_of_the_same_game_are_criar_aposta() -> None:
    eventos = [
        _evento(evento_id="777", escolha="Mais de 2.5"),
        _evento(evento_id="777", escolha="Ambas marcam"),
    ]

    lida = superbet.ler(_ticket(eventos=eventos))

    assert lida.tipo == "CRIAR_APOSTA"
    assert lida.descricao == "Mais de 2.5 (Super Odds) + Ambas marcam (Super Odds)"


def test_two_selections_of_different_games_are_multipla() -> None:
    eventos = [_evento(evento_id="777"), _evento(evento_id="888")]

    assert superbet.ler(_ticket(eventos=eventos)).tipo == "MULTIPLA"


def test_multipla_counts_on_the_earliest_game_not_the_first_in_the_json() -> None:
    eventos = [
        _evento(evento_id="777", date="2026-08-05T22:30:00.000Z"),
        _evento(evento_id="888", date="2026-08-03T19:00:00.000Z"),
    ]

    lida = superbet.ler(_ticket(eventos=eventos))

    assert lida.comeca_em == "2026-08-03T16:00:00"
    assert lida.data_aposta == "2026-08-03T16:00:00"
