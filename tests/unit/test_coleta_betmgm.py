from __future__ import annotations

import pytest

from bancaemdia.coleta import betmgm
from bancaemdia.coleta.leitura import ColetaInvalidaError


def _escolha(
    *,
    nome="Grêmio com mais posse de bola",
    odd="3.00",
    estado="LOST",
    mercado="GRE x BOL - Tudo ou nada pela vaga!",
):
    return {
        "id": "da300930-2d8c-34b2-b228-d6a9ae72aeb4",
        "odds": odd,
        "oddsType": "PLACEMENT_ODDS",
        "requestedOdds": odd,
        "status": estado,
        "type": "Gremio to have more ball possesion",
        "name": nome,
        "live": False,
        "marketId": "764d556e-4003-3f01-a82c-94ac65970dce",
        "marketType": {"type": "free-text", "name": mercado},
        "outcomeType": "REGULAR",
        "deductionFactor": 0.0,
        "deductions": [],
        "tags": [],
        "eachWay": None,
    }


def _evento(
    *,
    evento_id="1793277",
    nome="Longo Prazo",
    participantes=None,
    escolhas=None,
    tipo="OUTRIGHT",
    **extra,
):
    evento = {
        "id": evento_id,
        "type": tipo,
        "name": nome,
        "startsAt": "2026-07-30T22:00:00Z",
        "originalScheduledTime": None,
        "participants": participantes if participantes is not None else [],
        "outcomes": escolhas if escolhas is not None else [_escolha()],
        "score": None,
        "ongoing": False,
        "group": {"id": 6214, "name": "MGM Exclusives", "parent": None},
    }
    evento.update(extra)
    return evento


def _aposta(*, stake=8000, odd="3.00", estados=None, payout=0, **extra):
    aposta = {
        "type": "SINGLE",
        "uuid": "98bff8fe-9fb7-4c62-823c-49e982ea6cf3",
        "displayStatuses": estados if estados is not None else ["LOST"],
        "stake": stake,
        "originStake": stake,
        "possibleWinnings": 0,
        "payout": payout,
        "rejectedPayout": 0,
        "totalOdds": odd,
        "requestedTotalOdds": odd,
        "cashoutOffer": {"value": 0, "allowed": False},
        "cashoutHistory": [],
        "combinations": [],
        "tags": [],
    }
    aposta.update(extra)
    return aposta


def _bilhete(
    uuid_="a1b7d175-3b16-42af-a5f3-9cf1b1369c00",
    *,
    eventos=None,
    apostas=None,
    stake=8000,
    odd="3.00",
    estados=None,
    payout=0,
    entregue="2026-07-29T17:03:05.206Z",
    **extra,
):
    bruto = {
        "uuid": uuid_,
        "options": {"acceptOddsChanges": False},
        "status": "CLOSED",
        "oddsFormat": "DECIMAL",
        "currency": "BRL",
        "channel": "DESKTOP",
        "deliveryDate": entregue,
        "updateDate": "2026-07-31T00:01:15.343Z",
        "events": eventos if eventos is not None else [_evento()],
        "transactions": [{"type": "WITHDRAW_FUNDS", "amount": stake, "balanceBefore": 46020}],
        "selectionLength": 1,
        "type": "SINGLE",
        "bets": (
            apostas
            if apostas is not None
            else [_aposta(stake=stake, odd=odd, estados=estados, payout=payout)]
        ),
        "stake": stake,
        "originStake": stake,
        "possibleWinnings": 0,
        "payout": payout,
        "location": "BR",
        "version": 4,
        "betRequest": False,
        "prebuilt": [],
    }
    bruto.update(extra)
    return bruto


def test_top_level_fields_become_the_bet() -> None:
    lida = betmgm.ler(_bilhete())

    assert lida.casa == "betmgm"
    assert lida.identidade == "a1b7d175-3b16-42af-a5f3-9cf1b1369c00"
    assert lida.odd == pytest.approx(3.0)
    assert lida.tipo == "SIMPLES"


def test_stake_already_comes_in_centavos() -> None:
    assert betmgm.ler(_bilhete(stake=8000)).stake_centavos == 8000


def test_odd_sent_as_text_becomes_a_number() -> None:
    assert betmgm.ler(_bilhete(odd="3.00")).odd == pytest.approx(3.0)


def test_odd_that_does_not_convert_is_refused_and_never_becomes_zero() -> None:
    with pytest.raises(ColetaInvalidaError, match="odd"):
        betmgm.ler(_bilhete(odd="três"))


def test_game_date_in_utc_becomes_local_time() -> None:
    lida = betmgm.ler(_bilhete(eventos=[_evento(startsAt="2026-07-29T17:03:05.206Z")]))

    assert lida.data_aposta == "2026-07-29T14:03:05"


def test_delivery_date_is_the_fallback_and_also_converts() -> None:
    evento = _evento()
    del evento["startsAt"]

    lida = betmgm.ler(_bilhete(eventos=[evento], entregue="2026-07-29T17:03:05.206Z"))

    assert lida.comeca_em is None
    assert lida.data_aposta == "2026-07-29T14:03:05"


def test_bet_counts_on_the_game_date_not_the_delivery() -> None:
    lida = betmgm.ler(
        _bilhete(
            eventos=[_evento(startsAt="2026-08-05T22:00:00Z")],
            entregue="2026-07-29T17:03:05.206Z",
        )
    )

    assert lida.data_aposta.startswith("2026-08-05")


def test_participants_become_the_matchup_when_present() -> None:
    participantes = [{"id": 1, "name": "Grêmio"}, {"id": 2, "name": "Bolívar"}]

    lida = betmgm.ler(_bilhete(eventos=[_evento(participantes=participantes)]))

    assert lida.evento == "Grêmio x Bolívar"


def test_without_participants_the_event_name_is_used() -> None:
    assert betmgm.ler(_bilhete()).evento == "Longo Prazo"


def test_lost_bet_returns_zero() -> None:
    lida = betmgm.ler(_bilhete(estados=["LOST"]))

    assert (lida.estado, lida.retorno_centavos) == ("RED", 0)


def test_won_bet_carries_the_payout_in_centavos() -> None:
    lida = betmgm.ler(_bilhete(estados=["WON"], stake=8000, payout=24000))

    assert (lida.estado, lida.retorno_centavos) == ("GREEN", 24000)
    assert lida.motivo_retencao is None


@pytest.mark.parametrize("estado", ["OPEN", "PENDING", "PLACED"])
def test_running_statuses_stay_pending_without_a_reason(estado) -> None:
    lida = betmgm.ler(_bilhete(estados=[estado]))

    assert (lida.estado, lida.retorno_centavos, lida.motivo_retencao) == ("PENDENTE", None, None)


def test_two_statuses_at_once_hold_the_bet() -> None:
    lida = betmgm.ler(_bilhete(estados=["WON", "CASHED_OUT"]))

    assert lida.estado == "PENDENTE"
    assert "mais de um estado" in (lida.motivo_retencao or "")


def test_unknown_status_is_not_guessed() -> None:
    lida = betmgm.ler(_bilhete(estados=["ANULADA_PELA_CASA"]))

    assert lida.estado == "PENDENTE"
    assert "ANULADA_PELA_CASA" in (lida.motivo_retencao or "")


def test_ticket_with_several_bets_inside_is_held() -> None:
    uma = _aposta()

    lida = betmgm.ler(_bilhete(apostas=[uma, dict(uma)]))

    assert "2 apostas dentro" in (lida.motivo_retencao or "")


def test_currency_other_than_brl_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="reais"):
        betmgm.ler(_bilhete(currency="USD"))


def test_bet_without_uuid_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="identificador"):
        betmgm.ler(_bilhete(uuid_=""))


def test_cashout_without_payout_is_held() -> None:
    aposta = {
        "type": "SINGLE",
        "uuid": "98bff8fe",
        "displayStatuses": ["WON"],
        "stake": 8000,
        "totalOdds": "3.00",
        "payout": None,
        "cashoutHistory": [{"amount": 5000}],
    }

    lida = betmgm.ler(_bilhete(apostas=[aposta]))

    assert (lida.estado, lida.retorno_centavos) == ("CASHOUT", None)
    assert "não mandou o valor" in (lida.motivo_retencao or "")


def test_cashout_with_payout_keeps_the_value_in_centavos() -> None:
    aposta = {
        "type": "SINGLE",
        "uuid": "98bff8fe",
        "displayStatuses": ["WON"],
        "stake": 8000,
        "totalOdds": "3.00",
        "payout": 5000,
        "cashoutHistory": [{"amount": 5000}],
    }

    lida = betmgm.ler(_bilhete(apostas=[aposta]))

    assert (lida.estado, lida.retorno_centavos) == ("CASHOUT", 5000)
    assert lida.motivo_retencao is None
