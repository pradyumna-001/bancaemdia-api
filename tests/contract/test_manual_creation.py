"""Complete generated API contract for manual identity/recovery and legacy compatibility."""

import json
from pathlib import Path

from bancaemdia.domain.manual_contract import ManualCreationRequest
from bancaemdia.services.manual_creation import fingerprint

SCHEMA = Path(__file__).parent / "schemas/openapi.json"
P = "/api/v1/apostas-manuais/operacoes/{intencao_id}"


def test_manual_identity_is_published_in_both_operations_and_has_no_path_collision():
    d = json.loads(SCHEMA.read_text(encoding="utf8"))
    assert set(d["paths"][P]) == {"post", "get"}
    assert "/api/v1/apostas/{chave}" in d["paths"]
    for op in d["paths"][P].values():
        identity = next(v for v in op["parameters"] if v["name"] == "intencao_id")
        assert identity["in"] == "path" and identity["schema"]["format"] == "uuid"
        assert "READ_ONLY" in d["paths"][P]["get"]["description"]


def test_manual_openapi_retention_no_retry_and_cookie_permissions():
    d = json.loads(SCHEMA.read_text(encoding="utf8"))
    post, get = d["paths"][P]["post"], d["paths"][P]["get"]
    assert post["x-bancaemdia-policy"] == {
        "access_modes": ["FULL_WRITE"],
        "automatic_retry": {"allowed": False, "max_attempts": None},
        "idempotency": {
            "identity": "path:intencao_id",
            "retention": "until_account_deletion",
            "recovery": "GET " + P,
        },
    }
    assert "90 dias" in post["description"]
    assert "Nenhum POST recebe retry automático" in post["description"]
    assert get["x-bancaemdia-policy"]["access_modes"] == ["FULL_WRITE", "READ_ONLY"]
    assert {"SessionCookie": [], "CsrfProof": []} in post["security"]
    assert {"SessionCookie": []} in get["security"]
    assert {"200", "201", "401", "402", "403", "409", "410", "422", "429", "503"} <= post[
        "responses"
    ].keys()


def test_manual_typed_receipt_safe_errors_exact_ids_and_money():
    schemas = json.loads(SCHEMA.read_text(encoding="utf8"))["components"]["schemas"]
    request = schemas["ManualCreationRequest"]["properties"]
    for field in ("conta_casa_id", "comissao_centavos"):
        assert request[field]["anyOf"][0]["type"] == "string"
    original = schemas["ManualOriginalConfirmation"]["properties"]
    for field in (
        "stake_centavos",
        "valor_aposta_centavos",
        "comissao_centavos",
        "valor_unidade_centavos",
    ):
        assert original[field]["type"] == "string"
    assert set(schemas["ManualCreationError"]["properties"]) == {"detail", "campo", "efeito"}
    assert schemas["ManualCreationReceipt"]["additionalProperties"] is False
    assert schemas["ManualAssignmentNotice"]["properties"]["codigo"]["enum"] == [
        "conta_ausente",
        "conta_ambigua",
    ]


def test_legacy_manual_contract_still_available_without_requiring_identity():
    d = json.loads(SCHEMA.read_text(encoding="utf8"))
    post = d["paths"]["/api/v1/apostas"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApostaManual"
    )
    assert not any(
        p["name"] in {"intencao_id", "Idempotency-Key"} for p in post.get("parameters", [])
    )


def test_fingerprint_normalizes_only_input_never_time_or_registry_defaults():
    a = ManualCreationRequest(
        casa="betano", odd=2, stake_unidades=1, data_jogo="2026-10-15T12:00:00"
    )
    b = ManualCreationRequest(
        casa="Betano",
        odd=2.0,
        stake_unidades=1.0,
        data_jogo="2026-10-15T15:00:00Z",
        comissao_centavos="0",
    )
    assert fingerprint(a) == fingerprint(b)
    assert a.data_aposta is None
    assert fingerprint(a) != fingerprint(a.model_copy(update={"descricao": "Another bet"}))
