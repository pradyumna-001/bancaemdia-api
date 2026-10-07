import json

from bancaemdia.domain.cruzamento import compare, normalize
from bancaemdia.services.cruzamento_candidatos import matching_originals


def test_evidence_minimization_does_not_copy_nested_credentials_or_notes():
    sentinel = "token-and-personal-note@example.invalid"
    state = {
        "casa": "Betano",
        "identidade_bilhete": "private-ticket",
        "cookie": sentinel,
        "selecoes": [
            {
                "evento": "Azul x Verde",
                "mercado": "Gols",
                "escolha": "Mais",
                "linha": 2.5,
                "authorization": sentinel,
                "notes": sentinel,
            }
        ],
    }
    original = matching_originals(state)
    assert sentinel not in json.dumps(original)
    assert original["identidade_bilhete"] == "private-ticket"
    assert original["selecoes"] == [
        {"evento": "Azul x Verde", "mercado": "Gols", "escolha": "Mais", "linha": 2.5}
    ]
    result = compare(normalize(original), normalize(original))
    assert "private-ticket" not in result.explanation
    assert result == compare(normalize(original), normalize(original))


def test_minimization_preserves_the_multiple_selection_budget_veto():
    leg = {"evento": "Azul x Verde", "mercado": "Gols", "escolha": "Mais"}
    normalized = normalize(matching_originals({"selecoes": [leg] * 50}))
    assert normalized["estrutura_completa"] is False
