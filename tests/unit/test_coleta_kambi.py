from __future__ import annotations

import pytest

from bancaemdia.coleta import kambi
from bancaemdia.coleta.leitura import ColetaInvalidaError


def _evento(*, evento_id=1025985635, nome="Guarani - Athletico", comeca="2026-08-15T19:30:00Z"):
    return {
        "eventId": evento_id,
        "eventName": nome,
        "homeName": "Guarani",
        "awayName": "Athletico",
        "eventStartDate": comeca,
        "sport": "FOOTBALL",
        "eventGroups": [
            {"id": 1000093190, "name": "Futebol"},
            {"id": 1000461741, "name": "Brasil"},
            {"id": 1000094569, "name": "Brasileirão Série A"},
        ],
    }


def _escolha(
    *,
    outcome_id=4286838316,
    oferta_id=2677128807,
    evento_id=1025985635,
    rotulo="Jogador X - Mais 0.5",
    linha=500,
    estado="LOST",
):
    escolha = {
        "outcomeId": outcome_id,
        "eventId": evento_id,
        "betOfferId": oferta_id,
        "label": rotulo,
        "settledInfo": {"result": {"score": "", "correct": [], "scratched": []}},
        "status": estado,
        "participantId": 1000780201,
        "outcomeTags": [],
        "earlySettlement": False,
    }
    if linha is not None:
        escolha["line"] = linha
    return escolha


def _oferta(
    *,
    oferta_id=2677128807,
    linha=500,
    criterio="Chutes a gol pelo jogador (Fechado usando dados Opta)",
):
    oferta = {
        "betOfferId": oferta_id,
        "boType": "Player Occurrence Line",
        "boTypeId": 127,
        "criterion": criterio,
        "tags": [],
    }
    if linha is not None:
        oferta["line"] = linha
    return oferta


def _linha(*, indice=0, outcome_id=4286838316, odds=1850, estado="LOST"):
    return {
        "index": indice,
        "status": estado,
        "outcomeId": outcome_id,
        "playedOdds": odds,
        "payoutOdds": 0,
        "bestOddsGuaranteed": False,
        "tags": [],
        "selectionType": "SIMPLE",
    }


def _aposta(*, bet_ref=16152868309, stake=80000, odds=1850, bet_odds=None, payout=0, estado="LOST"):
    return {
        "betRef": bet_ref,
        "couponRowIndexes": [0],
        "betOdds": odds if bet_odds is None else bet_odds,
        "playedOdds": odds,
        "betStatus": estado,
        "stake": stake,
        "payout": payout,
        "potentialPayout": payout,
        "tags": [],
    }


def _cupom(
    *,
    ref="647dfb90-8e6d-42bc-8b34-6127f94ec48a",
    stake=80000,
    odds=1850,
    bet_odds=None,
    payout=0,
    estado="LOST",
    escolhas=None,
    eventos=None,
    ofertas=None,
    linhas=None,
    apostas=None,
    colocado="2026-08-15T18:53:46.838Z",
    moeda="BRL",
):
    return {
        "couponRef": 13003243456,
        "couponExternalRef": ref,
        "placedDate": colocado,
        "channel": "WEB",
        "currency": moeda,
        "tags": [],
        "systemBets": [],
        "bets": apostas
        if apostas is not None
        else [_aposta(stake=stake, odds=odds, bet_odds=bet_odds, payout=payout, estado=estado)],
        "couponRows": linhas if linhas is not None else [_linha(odds=odds, estado=estado)],
        "outcomes": escolhas if escolhas is not None else [_escolha()],
        "betOffers": ofertas if ofertas is not None else [_oferta()],
        "events": eventos if eventos is not None else [_evento()],
    }


def _multipla(odds):
    return _cupom(
        odds=odds,
        linhas=[
            _linha(indice=0, outcome_id=1, odds=5000, estado="LOST"),
            _linha(indice=1, outcome_id=2, odds=2000, estado="WON"),
        ],
        escolhas=[_escolha(outcome_id=1, evento_id=111), _escolha(outcome_id=2, evento_id=222)],
        eventos=[_evento(evento_id=111), _evento(evento_id=222, nome="Sport - Ceara")],
    )


def test_top_level_fields_become_the_bet() -> None:
    lida = kambi.ler(_cupom())

    assert lida.casa == "kambi"
    assert lida.identidade == "13003243456"
    assert lida.odd == pytest.approx(1.85)
    assert lida.tipo == "SIMPLES"


def test_identity_is_the_coupon_ref_and_not_the_composite_external_ref() -> None:
    lida = kambi.ler(_cupom(ref="65ade90e-5a28-4c6d-9b1f-7e5c542b1c05-noPba"))

    assert lida.identidade == "13003243456"


def test_stake_comes_in_thousandths_of_a_real() -> None:
    assert kambi.ler(_cupom(stake=80000)).stake_centavos == 8000


def test_odd_and_payout_also_come_times_a_thousand() -> None:
    lida = kambi.ler(_cupom(odds=1640, stake=45000, payout=73800, estado="WON"))

    assert lida.odd == pytest.approx(1.64)
    assert (lida.stake_centavos, lida.retorno_centavos) == (4500, 7380)


def test_odd_never_comes_from_bet_odds() -> None:
    assert kambi.ler(_cupom(odds=1850, bet_odds=0, estado="LOST")).odd == pytest.approx(1.85)


def test_lost_bet_returns_zero() -> None:
    lida = kambi.ler(_cupom(estado="LOST", payout=0))

    assert (lida.estado, lida.retorno_centavos) == ("RED", 0)


def test_void_bet_returns_the_stake_and_keeps_the_house_reason() -> None:
    cupom = _cupom(estado="VOID", stake=126000, odds=2140, bet_odds=1000, payout=126000)
    cupom["outcomes"][0]["settledInfo"] = {"voidReason": "Adiado"}

    lida = kambi.ler(cupom)

    assert (lida.estado, lida.retorno_centavos) == ("ANULADA", 12600)
    assert lida.odd == pytest.approx(2.14)
    assert lida.observacao == "a casa anulou: Adiado"


def test_selections_on_the_same_event_are_a_bet_builder() -> None:
    lida = kambi.ler(
        _cupom(
            escolhas=[
                _escolha(outcome_id=1, evento_id=777, rotulo="Jogador A - Mais 0.5"),
                _escolha(outcome_id=2, evento_id=777, rotulo="Jogador B - Mais 1.5", linha=1500),
            ]
        )
    )

    assert lida.tipo == "CRIAR_APOSTA"


def test_distinct_events_are_a_multiple() -> None:
    assert kambi.ler(_multipla(10000)).tipo == "MULTIPLA"


def test_date_comes_in_utc_and_counts_by_the_game() -> None:
    lida = kambi.ler(
        _cupom(
            colocado="2026-08-15T18:53:46.838Z", eventos=[_evento(comeca="2026-08-16T19:30:00Z")]
        )
    )

    assert lida.comeca_em == "2026-08-16T16:30:00"
    assert lida.data_aposta == "2026-08-16T16:30:00"


def test_without_a_game_date_the_bet_counts_by_its_placement() -> None:
    lida = kambi.ler(_cupom(colocado="2026-08-15T18:53:46.838Z", eventos=[_evento(comeca=None)]))

    assert lida.comeca_em is None
    assert lida.data_aposta == "2026-08-15T15:53:46"


def test_multiple_counts_by_the_earliest_game_and_not_the_json_order() -> None:
    cupom = _multipla(10000)
    cupom["events"] = [
        _evento(evento_id=111, comeca="2026-08-05T22:30:00Z"),
        _evento(evento_id=222, nome="Sport - Ceara", comeca="2026-08-03T19:00:00Z"),
    ]

    assert kambi.ler(cupom).data_aposta == "2026-08-03T16:00:00"


def test_line_also_comes_times_a_thousand() -> None:
    lida = kambi.ler(_cupom(escolhas=[_escolha(linha=1500)], ofertas=[_oferta(linha=1500)]))

    assert lida.escolhas[0].linha == pytest.approx(1.5)


def test_market_comes_from_the_offer_criterion() -> None:
    lida = kambi.ler(_cupom())

    assert lida.escolhas[0].mercado == "Chutes a gol pelo jogador (Fechado usando dados Opta)"


def test_event_name_comes_from_the_event() -> None:
    assert kambi.ler(_cupom()).evento == "Guarani - Athletico"


def test_open_bet_is_pending_with_null_return_and_no_hold() -> None:
    lida = kambi.ler(_cupom(estado="OPEN", stake=1000, odds=1540, payout=0))

    assert (lida.estado, lida.retorno_centavos) == ("PENDENTE", None)
    assert lida.motivo_retencao is None
    assert lida.stake_centavos == 100


def test_open_selection_without_settled_info_or_line_is_read() -> None:
    escolha = {
        "outcomeId": 4289412705,
        "eventId": 1025985733,
        "betOfferId": 2677889854,
        "label": "Internacional",
        "status": "OPEN",
        "participantId": 1000089814,
        "outcomeTags": [],
        "earlySettlement": False,
    }

    lida = kambi.ler(
        _cupom(
            estado="OPEN",
            escolhas=[escolha],
            ofertas=[_oferta(linha=None, criterio="Resultado final")],
        )
    )

    assert lida.escolhas[0].descricao == "Internacional"
    assert lida.escolhas[0].linha is None
    assert lida.observacao is None


def test_won_bet_without_payout_is_held_instead_of_returning_zero() -> None:
    cupom = _cupom(estado="WON")
    del cupom["bets"][0]["payout"]

    lida = kambi.ler(cupom)

    assert (lida.estado, lida.retorno_centavos) == ("PENDENTE", None)
    assert lida.motivo_retencao == (
        "a casa marcou esta aposta como WON mas não mandou o valor pago — confira na casa"
    )


def test_unmeasured_status_is_held_with_the_house_text() -> None:
    lida = kambi.ler(_cupom(estado="CASHED_OUT", payout=50000))

    assert (lida.estado, lida.retorno_centavos) == ("PENDENTE", None)
    assert "CASHED_OUT" in lida.motivo_retencao


def test_early_settlement_does_not_become_cashout() -> None:
    cupom = _cupom(estado="WON", payout=148000)
    cupom["outcomes"][0]["earlySettlement"] = True

    assert kambi.ler(cupom).estado == "GREEN"


def test_coupon_with_more_than_one_bet_inside_is_held() -> None:
    lida = kambi.ler(
        _cupom(apostas=[_aposta(bet_ref=1, stake=40000), _aposta(bet_ref=2, stake=40000)])
    )

    assert "2 apostas" in lida.motivo_retencao


def test_currency_other_than_real_is_rejected() -> None:
    with pytest.raises(ColetaInvalidaError, match="reais"):
        kambi.ler(_cupom(moeda="EUR"))


def test_coupon_without_identity_is_rejected() -> None:
    cupom = _cupom()
    del cupom["couponExternalRef"]
    del cupom["couponRef"]

    with pytest.raises(ColetaInvalidaError, match="sem identificador"):
        kambi.ler(cupom)


def test_missing_stake_is_rejected_and_never_becomes_zero() -> None:
    cupom = _cupom()
    del cupom["bets"][0]["stake"]

    with pytest.raises(ColetaInvalidaError, match="valor apostado"):
        kambi.ler(cupom)


def test_multiple_checks_the_product_of_the_legs_against_the_total() -> None:
    assert kambi.ler(_multipla(10000)).motivo_retencao is None
    assert "não fecham" in kambi.ler(_multipla(12000)).motivo_retencao
