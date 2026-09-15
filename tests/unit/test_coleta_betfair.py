from __future__ import annotations

import pytest

from bancaemdia.coleta import betfair
from bancaemdia.coleta.leitura import ColetaInvalidaError


def _parte(*, odd=4.5, evento_id=35897306, evento="Gremio x Mirassol", **campos):
    parte = {
        "marketBetUrn": "ppb:marketBet:924.531637596",
        "marketId": "924.531637596",
        "sportId": "1",
        "eventUrn": f"ppb:event:{evento_id}",
        "price": {"decimal": odd, "__typename": "SportsbookOdds"},
        "originalPrice": {"decimal": odd},
        "priceType": "LIVE",
        "eventDescription": evento,
        "eventMarketDescription": "Mercados de cartão vermelho",
        "marketType": "RED_CARD_MARKETS",
        "selectionId": 12376444,
        "selectionName": "Um cartão vermelho na partida",
        "handicap": None,
        "rule4Deductions": 0,
        "deadHeatWinDeductions": 0,
        "participants": [],
        "__typename": "LegPart",
    }
    parte.update(campos)
    return parte


def _grupo(
    bet_id="2681269229",
    *,
    stake=39.95,
    odd=4.5,
    resultado="LOST",
    pl=0,
    linhas=1,
    partes=None,
    casa_time="Gremio",
    fora_time="Mirassol",
    evento_id=35897306,
    comeca="2026-08-05T22:31:14.000Z",
    **campos,
):
    aposta = {
        "urn": f"ppb:sbkBet:{bet_id}",
        "__typename": "SportsbookBet",
        "betReceiptId": "O/22629919/0001195",
        "id": bet_id,
        "isSettled": True,
        "profitAndLoss": pl,
        "isOddsBoosted": False,
        "betType": "SGL",
        "isEachWay": False,
        "isSGM": False,
        "currentSize": stake,
        "numLines": linhas,
        "currentSizePerLine": stake,
        "result": resultado,
        "resultType": "CONFIRMED",
        "cashoutQuote": None,
        "bonus": 0,
        "product": "SPORTSBOOK",
        "betPrice": None,
        "originalBetPrice": None,
        "legs": [{"urn": f"ppb:sbkBetLeg:{bet_id}/0", "__typename": "BetLeg"}],
        "lowestEventStartTime": comeca,
    }
    aposta.update(campos)
    if partes is None:
        partes = [_parte(odd=odd, evento_id=evento_id, evento=f"{casa_time} x {fora_time}")]
    return {
        "__typename": "BetCardGroup",
        "urn": f"ppb:tbd:card:bet:group:{bet_id}|sbk",
        "full": {
            "edges": [
                {"node": {"__typename": "SportsbookBetCard", "bet": aposta}},
                {
                    "node": {
                        "__typename": "SportsbookExpandableLegCardGroup",
                        "full": {
                            "edges": [
                                {
                                    "node": {
                                        "__typename": "SportsbookBetLegCardGroup",
                                        "full": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "__typename": "BetLegCard",
                                                        "leg": {
                                                            "__typename": "BetLeg",
                                                            "type": "SS",
                                                            "result": resultado,
                                                            "legNumber": 1,
                                                            "parts": partes,
                                                        },
                                                    }
                                                }
                                            ]
                                        },
                                    }
                                }
                            ]
                        },
                    }
                },
                {
                    "node": {
                        "__typename": "FixtureCard",
                        "fixture": {
                            "__typename": "FootballFixture",
                            "home": {"name": casa_time},
                            "away": {"name": fora_time},
                            "scheduledAt": comeca,
                            "score": {"home": 1, "away": 0},
                        },
                        "sportevent": {
                            "__typename": "SportsEvent",
                            "eventId": evento_id,
                            "name": f"{casa_time} x {fora_time}",
                            "openDate": comeca,
                            "sport": {"name": "Futebol", "sportId": 1},
                        },
                    }
                },
            ]
        },
    }


def _perna_da_multipla(urn_evento, evento, odd, escolha):
    return {
        "__typename": "BetLegCard",
        "leg": {
            "__typename": "BetLeg",
            "parts": [
                {
                    "__typename": "LegPart",
                    "eventUrn": urn_evento,
                    "eventDescription": evento,
                    "eventMarketDescription": "Resultado final",
                    "marketType": "MATCH_ODDS",
                    "selectionName": escolha,
                    "price": {"decimal": odd, "__typename": "SportsbookOdds"},
                    "originalPrice": {"decimal": odd},
                    "handicap": None,
                    "participants": [],
                    "rule4Deductions": 0,
                }
            ],
        },
    }


def _jogo_da_multipla(evento_id, casa_time, fora_time):
    return {
        "__typename": "FixtureCard",
        "fixture": {
            "__typename": "FootballFixture",
            "home": {"name": casa_time},
            "away": {"name": fora_time},
            "score": {},
        },
        "sportevent": {"__typename": "SportsEvent", "eventId": evento_id},
    }


def _multipla(**campos):
    aposta = {
        "urn": "ppb:sbkBet:2683564391",
        "__typename": "SportsbookBet",
        "betReceiptId": "O/22629919/0001216",
        "id": "2683564391",
        "isSettled": False,
        "profitAndLoss": 1.6,
        "isOddsBoosted": False,
        "betType": "DBL",
        "isEachWay": False,
        "isSGM": False,
        "isSGMMulti": False,
        "currentSize": 1,
        "numLines": 1,
        "currentSizePerLine": 1,
        "result": "WINNING",
        "resultType": "POTENTIAL",
        "cashoutQuote": {
            "__typename": "SportsbookCashoutQuote",
            "quote": 0.78,
            "status": "AVAILABLE",
            "stake": 1,
            "refreshRate": 5,
            "betDelay": 1,
        },
        "bonus": 0,
        "product": "SPORTSBOOK",
        "betPrice": {"__typename": "SportsbookOdds", "decimal": 1.6},
        "originalBetPrice": None,
        "legs": [
            {"urn": "ppb:sbkBetLeg:2683564391/0", "__typename": "BetLeg"},
            {"urn": "ppb:sbkBetLeg:2683564391/1", "__typename": "BetLeg"},
        ],
        "lowestEventStartTime": "2026-08-11T01:02:53.000Z",
    }
    aposta.update(campos)

    def grupo_da_perna(perna, jogo):
        return {
            "node": {
                "__typename": "SportsbookBetLegCardGroup",
                "full": {"edges": [{"node": perna}, {"node": jogo}]},
            }
        }

    return {
        "__typename": "BetCardGroup",
        "urn": "ppb:tbd:card:bet:group:2683564391|sbk",
        "full": {
            "edges": [
                {"node": {"__typename": "SportsbookBetCard", "bet": aposta}},
                {
                    "node": {
                        "__typename": "SportsbookExpandableLegCardGroup",
                        "full": {
                            "edges": [
                                grupo_da_perna(
                                    _perna_da_multipla(
                                        "ppb:event:35925417",
                                        "Pitbulls x Guadalupe F.C",
                                        1.14,
                                        "Empate",
                                    ),
                                    _jogo_da_multipla("35925417", "Pitbulls", "Guadalupe F.C"),
                                ),
                                grupo_da_perna(
                                    _perna_da_multipla(
                                        "ppb:event:35922844",
                                        "León (F) x Tigres UANL (F)",
                                        1.4,
                                        "Empate",
                                    ),
                                    _jogo_da_multipla("35922844", "León (F)", "Tigres UANL (F)"),
                                ),
                            ]
                        },
                    }
                },
            ]
        },
    }


def _aposta(grupo):
    return grupo["full"]["edges"][0]["node"]["bet"]


def test_reads_the_bet_card_group_fields() -> None:
    lida = betfair.ler(_grupo())

    assert lida.casa == "betfair"
    assert lida.identidade == "2681269229"
    assert lida.stake_centavos == 3995
    assert lida.odd == pytest.approx(4.5)
    assert lida.evento == "Gremio x Mirassol"
    assert lida.mercado_bruto == "Mercados de cartão vermelho"
    assert lida.escolhas[0].descricao == "Um cartão vermelho na partida"
    assert lida.observacao == "Resultado 1-0"


def test_finds_the_cards_by_typename_not_by_position() -> None:
    grupo = _grupo()
    grupo["full"]["edges"].reverse()

    lida = betfair.ler(grupo)

    assert lida.identidade == "2681269229"
    assert lida.evento == "Gremio x Mirassol"


def test_lost_bet_returns_zero() -> None:
    lida = betfair.ler(_grupo(resultado="LOST", pl=0))

    assert (lida.estado, lida.retorno_centavos) == ("RED", 0)


def test_profit_and_loss_matching_the_return_is_the_return() -> None:
    lida = betfair.ler(_grupo(stake=100.0, odd=2.0, resultado="WON", pl=200.0))

    assert (lida.estado, lida.retorno_centavos) == ("GREEN", 20000)
    assert lida.motivo_retencao is None


def test_profit_and_loss_matching_the_profit_adds_the_stake_back() -> None:
    lida = betfair.ler(_grupo(stake=100.0, odd=2.0, resultado="WON", pl=100.0))

    assert (lida.estado, lida.retorno_centavos) == ("GREEN", 20000)
    assert lida.motivo_retencao is None


def test_profit_and_loss_matching_neither_reading_is_retained_with_the_numbers() -> None:
    lida = betfair.ler(_grupo(stake=100.0, odd=2.0, resultado="WON", pl=137.0))

    assert lida.estado == "GREEN"
    assert "não bate" in (lida.motivo_retencao or "")
    assert "137" in (lida.motivo_retencao or "")


def test_settled_win_stays_green() -> None:
    lida = betfair.ler(_grupo(resultado="WON", pl=179.78, stake=39.95, odd=4.5))

    assert (lida.estado, lida.retorno_centavos) == ("GREEN", 17978)


def test_several_selections_without_total_odd_are_retained() -> None:
    lida = betfair.ler(_grupo(partes=[_parte(), _parte()]))

    assert "não mandou a odd total" in (lida.motivo_retencao or "")


def test_bet_with_several_lines_is_retained() -> None:
    lida = betfair.ler(_grupo(linhas=3))

    assert "3 linhas" in (lida.motivo_retencao or "")


def test_cashout_quote_on_a_running_bet_does_not_settle_it() -> None:
    grupo = _grupo(resultado=None, pl=0)
    _aposta(grupo)["cashoutQuote"] = 38.40
    _aposta(grupo)["isSettled"] = False

    lida = betfair.ler(grupo)

    assert lida.estado == "PENDENTE"
    assert lida.retorno_centavos is None


def test_settled_cashout_is_read_as_cashout() -> None:
    grupo = _grupo(resultado="CASHED_OUT", pl=41.20)
    _aposta(grupo)["cashoutQuote"] = 41.20
    _aposta(grupo)["isSettled"] = True

    lida = betfair.ler(grupo)

    assert (lida.estado, lida.retorno_centavos) == ("CASHOUT", 4120)


def test_settled_cashout_with_the_value_is_not_retained() -> None:
    lida = betfair.ler(
        _grupo(
            resultado="WON",
            pl=88.0,
            resultType="CONFIRMED",
            cashoutQuote={"quote": 0.9, "status": "TAKEN"},
        )
    )

    assert (lida.estado, lida.retorno_centavos) == ("CASHOUT", 8800)
    assert lida.motivo_retencao is None


def test_cashout_without_profit_and_loss_is_retained() -> None:
    lida = betfair.ler(
        _grupo(resultado="WON", pl=None, cashoutQuote={"quote": 0.9, "status": "TAKEN"})
    )

    assert (lida.estado, lida.retorno_centavos) == ("CASHOUT", None)
    assert "não mandou o valor" in (lida.motivo_retencao or "")


def test_unknown_result_is_retained_as_pending() -> None:
    lida = betfair.ler(_grupo(resultado="VOID_BY_HOUSE"))

    assert lida.estado == "PENDENTE"
    assert "VOID_BY_HOUSE" in (lida.motivo_retencao or "")


def test_group_without_the_bet_card_is_refused() -> None:
    grupo = _grupo()
    grupo["full"]["edges"] = [
        e for e in grupo["full"]["edges"] if e["node"]["__typename"] != "SportsbookBetCard"
    ]

    with pytest.raises(ColetaInvalidaError, match="SportsbookBetCard"):
        betfair.ler(grupo)


def test_json_from_another_platform_is_refused_in_portuguese() -> None:
    bilhete = {
        "id": "20753556039",
        "totalAmount": 160.0,
        "placedAt": 1785708161930,
        "legs": [{"legItems": [{"eventId": "1", "selections": [{"description": "Mais de 2.5"}]}]}],
    }

    with pytest.raises(ColetaInvalidaError) as erro:
        betfair.ler(bilhete)

    assert "{" not in str(erro.value)
    assert "Error" not in str(erro.value)


def test_running_multiple_is_pending_even_with_a_cashout_quote() -> None:
    lida = betfair.ler(_multipla())

    assert lida.estado == "PENDENTE"
    assert lida.retorno_centavos is None


def test_disagreeing_end_markers_are_retained_as_pending() -> None:
    lida = betfair.ler(_grupo(resultado="WON", pl=100.0, resultType="POTENTIAL", isSettled=True))

    assert lida.estado == "PENDENTE"
    assert "POTENTIAL" in (lida.motivo_retencao or "")


def test_each_selection_carries_its_own_event() -> None:
    lida = betfair.ler(_multipla())

    assert [e.evento_id for e in lida.escolhas] == ["35925417", "35922844"]
    assert [e.evento for e in lida.escolhas] == [
        "Pitbulls x Guadalupe F.C",
        "León (F) x Tigres UANL (F)",
    ]
    assert lida.tipo == "MULTIPLA"


def test_single_selection_stays_simple() -> None:
    lida = betfair.ler(_grupo())

    assert lida.tipo == "SIMPLES"
    assert len(lida.escolhas) == 1
    assert lida.escolhas[0].evento_id == "35897306"


def test_leg_without_event_falls_back_to_the_fixture_card() -> None:
    parte = _parte()
    del parte["eventUrn"]
    del parte["eventDescription"]

    lida = betfair.ler(_grupo(partes=[parte]))

    assert lida.escolhas[0].evento_id == "35897306"
    assert lida.escolhas[0].evento == "Gremio x Mirassol"


def test_declared_total_odd_is_used() -> None:
    lida = betfair.ler(_multipla())

    assert lida.odd == pytest.approx(1.6)
    assert lida.motivo_retencao is None


def test_multiple_without_declared_total_odd_is_retained() -> None:
    lida = betfair.ler(_multipla(betPrice=None))

    assert "odd total" in (lida.motivo_retencao or "")


def test_bet_counts_on_the_earliest_event_start_and_fills_starts_at() -> None:
    lida = betfair.ler(_grupo(comeca="2026-08-05T22:31:14.000Z"))

    assert lida.data_aposta == "2026-08-05T19:31:14"
    assert lida.comeca_em == lida.data_aposta
