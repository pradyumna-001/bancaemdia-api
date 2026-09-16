from __future__ import annotations

import pytest

from bancaemdia.coleta import leitura
from bancaemdia.coleta.leitura import ColetaInvalidaError, Escolha


def _escolha(evento_id=None, descricao="Mais de 2.5", mercado="Gols"):
    return Escolha(descricao=descricao, mercado=mercado, evento_id=evento_id)


def test_epoch_in_milliseconds_becomes_local_time_without_offset() -> None:
    assert leitura.instante(1785708161930, "placedAt") == "2026-08-02T19:02:41"


@pytest.mark.parametrize("valor", [None, "1785708161930", 0, -1])
def test_epoch_that_is_not_a_positive_number_is_refused(valor) -> None:
    with pytest.raises(ColetaInvalidaError, match="placedAt"):
        leitura.instante(valor, "placedAt")


def test_iso_instant_in_utc_becomes_local_time() -> None:
    assert leitura.instante_iso("2026-08-02T15:35:38.721Z", "date") == "2026-08-02T12:35:38"
    assert leitura.instante_iso("2026-08-02T15:35:38-03:00", "date") == "2026-08-02T15:35:38"


def test_iso_instant_without_timezone_is_refused_instead_of_guessed() -> None:
    with pytest.raises(ColetaInvalidaError, match="sem fuso"):
        leitura.instante_iso("2026-08-02T15:35:38", "date")


@pytest.mark.parametrize("valor", [None, "", "  ", "ontem", 1785708161930])
def test_iso_instant_that_cannot_be_read_is_refused(valor) -> None:
    with pytest.raises(ColetaInvalidaError, match="não veio como instante"):
        leitura.instante_iso(valor, "date")


def test_earliest_game_wins_whatever_the_order() -> None:
    assert leitura.mais_cedo(["2026-08-03T10:00:00", None, "2026-08-02T21:00:00"]) == (
        "2026-08-02T21:00:00"
    )
    assert leitura.mais_cedo([None]) is None


def test_game_date_counts_and_placement_is_the_fallback() -> None:
    assert leitura.data_que_conta("2026-08-05T20:00:00", "2026-08-02T10:00:00") == (
        "2026-08-05T20:00:00"
    )
    assert leitura.data_que_conta(None, "2026-08-02T10:00:00") == "2026-08-02T10:00:00"


def test_only_the_result_or_the_settlement_says_a_bet_is_over() -> None:
    assert leitura.ja_acabou({}) is False
    assert leitura.ja_acabou({"cashoutStatus": 1, "cashoutAmount": 71.36}) is False
    assert leitura.ja_acabou({"finalBetResult": "Win"}) is True
    assert leitura.ja_acabou({"settledAt": 1785716993030}) is True


def test_house_field_names_come_as_parameters() -> None:
    assert leitura.ja_acabou({"result": "WON"}, resultado_em="result", liquidada_em="isSettled")
    assert not leitura.ja_acabou(
        {"finalBetResult": "Win"}, resultado_em="result", liquidada_em="isSettled"
    )


def test_reais_become_centavos() -> None:
    assert leitura.centavos(160.0, "totalAmount") == 16000
    assert leitura.centavos(0.95, "finalWinnings") == 95


def test_money_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ColetaInvalidaError, match="não veio como número"):
        leitura.centavos("160.00", "totalAmount")


def test_type_comes_from_the_events_not_from_the_house_label() -> None:
    assert leitura.tipo_da_aposta([_escolha("1")]) == "SIMPLES"
    assert leitura.tipo_da_aposta([_escolha("1"), _escolha("1")]) == "CRIAR_APOSTA"
    assert leitura.tipo_da_aposta([_escolha("1"), _escolha("2")]) == "MULTIPLA"
    assert leitura.tipo_da_aposta([_escolha(), _escolha()]) == "CRIAR_APOSTA"


def test_description_joins_choices_with_their_markets() -> None:
    escolhas = [_escolha(descricao="Mais de 2.5"), _escolha(descricao="Casa", mercado=None)]

    assert leitura.descricao_das_escolhas(escolhas) == "Mais de 2.5 (Gols) + Casa"
    assert leitura.descricao_das_escolhas([]) == leitura.SEM_ESCOLHA_LIDA
