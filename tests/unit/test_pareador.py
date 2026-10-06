from bancaemdia.domain.pareador import comparar

CASA = {
    "casa": "Betano",
    "evento": "Internacional - Corinthians",
    "data_aposta": "2026-08-02T19:30:00-03:00",
    "mercado_bruto": "Total de gols",
    "descricao": "Mais de 2.5 gols",
}
DICA = {
    "casa": "Betano",
    "evento": "Corinthians x Internacional",
    "comeca_em": "2026-08-02T19:30:00-03:00",
    "data_aposta": "2026-08-01T12:00:00-03:00",
    "mercado_bruto": "Total de gols",
    "descricao": "Mais de 2.5 gols",
}


def test_confirmed_bet_matches_in_both_directions() -> None:
    assert comparar(CASA, DICA).resultado == "igual"
    assert comparar(DICA, CASA).resultado == "igual"


def test_different_line_remains_under_review() -> None:
    assert comparar(CASA, {**DICA, "descricao": "Mais de 3.5 gols"}).resultado == "duvida"


def test_different_house_or_rematch_a_month_later_is_new() -> None:
    assert comparar(CASA, {**DICA, "casa": "Bet365"}).resultado == "nova"
    assert comparar(CASA, {**DICA, "comeca_em": "2026-09-02T19:30:00-03:00"}).resultado == "nova"


def test_unknown_game_date_cannot_confirm_or_count_as_a_distinct_bet() -> None:
    assert comparar(CASA, {**DICA, "comeca_em": None, "data_aposta": None}).resultado == "duvida"
