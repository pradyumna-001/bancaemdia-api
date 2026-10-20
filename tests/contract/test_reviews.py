"""The generated whole-app contract exposes every choice used by frontend #28."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract
P = "/api/v1/revisao-operacional"


@pytest.fixture
def schema():
    return json.loads((Path(__file__).parent / "schemas/openapi.json").read_text(encoding="utf8"))


def test_review_complete_surface_published_with_private_access_and_recovery(schema):
    expected = {
        "/fila": {"get"},
        "/revisoes/{revisao_id}": {"get"},
        "/revisoes/{revisao_id}/foto": {"get"},
        "/operacoes/{operacao_id}": {"get", "post"},
    }
    for suffix, methods in expected.items():
        path = schema["paths"][P + suffix]
        assert set(path) == methods
        for method in methods:
            policy = path[method]["x-bancaemdia-review"]
            assert policy["access"] == ("READ_ONLY" if method == "get" else "FULL_WRITE")
            assert policy["automatic_mutation_replay"] is False
            assert policy["receipt_days"] == 90
            assert policy["precondition"] == "opaque_evidence_cas"


def test_review_typed_safe_evidence_exact_ids_money_and_nullable_return(schema):
    schemas = schema["components"]["schemas"]
    view = schemas["ReviewView"]["properties"]
    assert "precondicao" in view and "acoes" in view and "encaminhamento" in view
    assert all(
        name not in view for name in ("extracao_bruta", "midia_hash", "ai_cost", "provider_debug")
    )
    source = schemas["SourceEvidence"]["properties"]
    assert {s["type"] for s in source["stake_centavos"]["anyOf"]} == {"string", "null"}
    assert {s["type"] for s in source["valor_aposta_centavos"]["anyOf"]} == {"string", "null"}
    assert {s["type"] for s in source["retorno_centavos"]["anyOf"]} == {"string", "null"}
    assert schemas["ReviewDecision"]["properties"]["revisao_id"]["type"] == "string"
    assert "conta_casa_id" in schemas["ReviewDecision"]["properties"]
    assert "status_na_abertura" in schemas["AccountEvidence"]["properties"]


def test_review_tipster_choice_and_legacy_optional_compatibility(schema):
    schemas = schema["components"]["schemas"]
    for name in ("ReviewDecision", "ResolucaoNova"):
        prop = schemas[name]["properties"]["tipster_choice"]
        enum = next(s["enum"] for s in prop["anyOf"] if "enum" in s)
        assert enum == ["casa", "telegram"]
    assert set(schemas["ReviewDecision"]["properties"]["acao"]["enum"]) == {
        "CORRIGIR",
        "DESCARTAR",
        "MESMA",
        "DISTINTAS",
        "RECUSAR_VINCULO",
    }
    assert schemas["ReviewDecision"]["additionalProperties"] is False
    # Existing form remains documented despite runtime extra=allow.
    assert {"odd", "stake_unidades", "conta_casa_id", "data_jogo"} <= set(
        schemas["CorrecaoDaRevisao"]["properties"]
    )


def test_review_age_policy_matches_actual_opened_timestamp_and_brazil_timezone(schema):
    for path in (P + "/fila", "/api/v1/revisao"):
        parameters = {p["name"]: p for p in schema["paths"][path]["get"]["parameters"]}
        for name in ("desde", "ate"):
            description = parameters[name]["description"]
            assert "RevisaoPendente.criado_em" in description and "America/Sao_Paulo" in description
        assert "inclusivo" in parameters["desde"]["description"]
        assert "exclusivo" in parameters["ate"]["description"]


def test_review_photo_is_bounded_private_binary_and_not_signed_redirect(schema):
    operation = schema["paths"][P + "/revisoes/{revisao_id}/foto"]["get"]
    assert set(operation["responses"]["200"]["content"]) == {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    }
    assert "302" not in operation["responses"]
    assert "20 MiB" in operation["description"]


def test_review_examples_and_public_errors_are_complete(schema):
    operation = schema["paths"][P + "/operacoes/{operacao_id}"]["post"]
    value = operation["requestBody"]["content"]["application/json"]["examples"]["default"]["value"]
    assert value["acao"] == "MESMA" and value["tipster_choice"] == "telegram"
    assert len(value["precondicao"]) == 64
    assert set(schema["components"]["schemas"]["ReviewError"]["properties"]) == {"detail", "campo"}
    assert {"401", "402", "409", "422", "503"} <= set(operation["responses"])
