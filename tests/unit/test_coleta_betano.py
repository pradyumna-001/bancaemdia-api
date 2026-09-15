from __future__ import annotations

import pytest

from bancaemdia.coleta import betano
from bancaemdia.coleta.leitura import ColetaInvalidaError

PLACED = 1785708161930
SETTLED = 1785716993030
START = 1785709800000


def _selecao(
    descricao="Mais de 41.5",
    mercado="Total de laterais",
    mercado_id=460,
    linha=41.5,
    odd=1.90,
    resultado="Lost",
):
    return {
        "id": "10124786431",
        "description": descricao,
        "odds": odd,
        "settled": True,
        "result": resultado,
        "marketId": "2887848664",
        "market": mercado,
        "marketTypeId": mercado_id,
        "offerTypes": [],
        "playerSubstitutions": [],
        "handicap": linha,
    }


def _item(evento_id="86389413", evento="Internacional - Corinthians", selecoes=None, **extra):
    return {
        "comboLegType": 0,
        "odds": 1.90,
        "eventId": evento_id,
        "eventName": evento,
        "participants": [
            {"id": 107300, "name": "Internacional"},
            {"id": 107253, "name": "Corinthians"},
        ],
        "startTime": START,
        "sportId": "FOOT",
        "selections": selecoes or [_selecao()],
        "settled": True,
        "result": "Lost",
        "finalScoreResult": "Resultado 2-0",
        **extra,
    }


def _bilhete(
    id_="20753556039", *, itens=None, total=160.0, odd=1.90, resultado="Lose", ganho=0.0, **extra
):
    bruto = {
        "cashoutAmount": 0.0,
        "cashoutStatus": 0,
        "bonusType": 0,
        "id": id_,
        "type": "SGL",
        "typeName": "Simples",
        "totalAmount": total,
        "totalAmountWithCurrency": {"amount": total, "currencyCode": "BRL"},
        "totalOdds": odd,
        "finalWinnings": ganho,
        "placedAt": PLACED,
        "betslipId": "20740033789",
        "finalBetResult": resultado,
        "legs": [{"id": "10124786431", "legItems": itens or [_item()]}],
        "settledAt": SETTLED,
        **extra,
    }
    if resultado is None:
        bruto.pop("finalBetResult")
        bruto.pop("settledAt")
    return bruto


def test_fields_come_from_the_four_levels() -> None:
    lida = betano.ler(_bilhete())

    assert (lida.identidade, lida.casa, lida.stake_centavos) == ("20753556039", "betano", 16000)
    assert lida.odd == pytest.approx(1.90)
    assert lida.evento == "Internacional - Corinthians"
    assert lida.mercado_bruto == "Total de laterais"
    assert lida.escolhas[0].linha == pytest.approx(41.5)
    assert lida.escolhas[0].mercado_id == 460
    assert lida.observacao == "Resultado 2-0"
    assert lida.descricao == "Mais de 41.5 (Total de laterais)"


def test_identity_is_the_ticket_and_not_the_betslip() -> None:
    assert betano.ler(_bilhete()).identidade != "20740033789"


def test_bet_counts_on_the_local_game_date() -> None:
    lida = betano.ler(_bilhete())

    assert lida.comeca_em == "2026-08-02T19:30:00"
    assert lida.data_aposta == lida.comeca_em


def test_earliest_game_of_a_multiple_is_the_date() -> None:
    itens = [_item(startTime=START + 86_400_000), _item(evento_id="2", startTime=START)]

    assert betano.ler(_bilhete(itens=itens)).data_aposta == "2026-08-02T19:30:00"


def test_bet_without_a_game_time_counts_on_the_placement_date() -> None:
    item = _item()
    del item["startTime"]

    assert betano.ler(_bilhete(itens=[item])).data_aposta == "2026-08-02T19:02:41"


def test_open_bet_is_pending() -> None:
    lida = betano.ler(_bilhete(resultado=None))

    assert (lida.estado, lida.retorno_centavos, lida.motivo_retencao) == ("PENDENTE", None, None)


@pytest.mark.parametrize(
    ("resultado", "ganho", "estado", "retorno"),
    [
        ("Lose", 0.0, "RED", 0),
        ("Win", 280.0, "GREEN", 28000),
        ("Cashout", 0.95, "CASHOUT", 95),
        ("Void", 160.0, "ANULADA", 16000),
    ],
)
def test_results_the_house_sends(resultado, ganho, estado, retorno) -> None:
    lida = betano.ler(_bilhete(resultado=resultado, ganho=ganho))

    assert (lida.estado, lida.retorno_centavos, lida.motivo_retencao) == (estado, retorno, None)


def test_cashout_offer_on_a_running_bet_is_not_a_cashout() -> None:
    for status, oferta in ((1, 160.0), (3, 0)):
        lida = betano.ler(
            _bilhete(resultado=None, cashoutStatus=status, cashoutAmount=oferta, settledAt=0)
        )

        assert (lida.estado, lida.retorno_centavos) == ("PENDENTE", None)


def test_cashout_offer_does_not_change_a_settled_result() -> None:
    lida = betano.ler(_bilhete(cashoutStatus=1, cashoutAmount=95.5))

    assert (lida.estado, lida.retorno_centavos) == ("RED", 0)


@pytest.mark.parametrize(
    ("resultado", "estado"), [("Win", "GREEN"), ("Cashout", "CASHOUT"), ("Void", "ANULADA")]
)
def test_missing_paid_value_is_held_instead_of_becoming_zero(resultado, estado) -> None:
    bruto = _bilhete(resultado=resultado)
    del bruto["finalWinnings"]

    lida = betano.ler(bruto)

    assert (lida.estado, lida.retorno_centavos) == (estado, None)
    assert "não mandou o valor" in (lida.motivo_retencao or "")


def test_lost_bet_is_zero_even_without_the_paid_value() -> None:
    bruto = _bilhete(resultado="Lose")
    del bruto["finalWinnings"]

    lida = betano.ler(bruto)

    assert (lida.estado, lida.retorno_centavos, lida.motivo_retencao) == ("RED", 0, None)


def test_zero_the_house_sent_is_kept() -> None:
    lida = betano.ler(_bilhete(total=1.0, resultado="Cashout", ganho=0.0))

    assert (lida.estado, lida.retorno_centavos, lida.motivo_retencao) == ("CASHOUT", 0, None)


def test_unknown_result_is_held_with_the_house_value() -> None:
    lida = betano.ler(_bilhete(resultado="HalfWin"))

    assert lida.estado == "PENDENTE"
    assert "'HalfWin'" in (lida.motivo_retencao or "")


def test_unknown_bonus_is_held_and_never_becomes_a_freebet() -> None:
    lida = betano.ler(_bilhete(bonusType=3))

    assert "bonusType=3" in (lida.motivo_retencao or "")


def test_currency_other_than_reais_is_refused() -> None:
    bruto = _bilhete()
    bruto["totalAmountWithCurrency"]["currencyCode"] = "EUR"

    with pytest.raises(ColetaInvalidaError, match="EUR"):
        betano.ler(bruto)


@pytest.mark.parametrize(
    ("campo", "motivo"),
    [
        ("id", "sem identificador"),
        ("totalAmount", "totalAmount"),
        ("placedAt", "placedAt"),
        ("totalOdds", "sem a odd total"),
    ],
)
def test_bet_missing_an_essential_field_is_refused_with_a_reason(campo, motivo) -> None:
    bruto = _bilhete()
    del bruto[campo]

    with pytest.raises(ColetaInvalidaError, match=motivo):
        betano.ler(bruto)


def test_choice_without_a_description_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="sem descrição"):
        betano.ler(_bilhete(itens=[_item(selecoes=[_selecao(descricao=" ")])]))


def test_bet_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="não veio como objeto"):
        betano.ler([_bilhete()])


def test_two_choices_of_the_same_game_are_a_bet_builder_whatever_the_label() -> None:
    selecoes = [
        _selecao("Menos de 2.5", "Total de Impedimentos", 351, 2.5, 1.85),
        _selecao("Menos de 7,5", "Total de Cartões", 65, 7.5, 1.85, "Won"),
    ]

    lida = betano.ler(_bilhete(odd=1.85, itens=[_item(selecoes=selecoes)]))

    assert lida.tipo == "CRIAR_APOSTA"
    assert lida.odd == pytest.approx(1.85)


def test_choices_of_different_games_are_a_multiple() -> None:
    itens = [_item(), _item(evento_id="99999999", evento="Chapecoense - Cruzeiro")]

    assert betano.ler(_bilhete(itens=itens)).tipo == "MULTIPLA"
