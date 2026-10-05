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


def test_legacy_inputs_without_user_and_stake_never_auto_match() -> None:
    assert comparar(CASA, DICA).resultado == "nova"
    assert comparar(DICA, CASA).resultado == "nova"


def test_unknown_owner_and_different_line_cannot_match() -> None:
    assert comparar(CASA, {**DICA, "descricao": "Mais de 3.5 gols"}).resultado == "nova"


def test_different_house_or_rematch_a_month_later_is_new() -> None:
    assert comparar(CASA, {**DICA, "casa": "Bet365"}).resultado == "nova"
    assert comparar(CASA, {**DICA, "comeca_em": "2026-09-02T19:30:00-03:00"}).resultado == "nova"


def test_unknown_owner_and_game_date_cannot_match() -> None:
    assert comparar(CASA, {**DICA, "comeca_em": None, "data_aposta": None}).resultado == "nova"
