import json
from pathlib import Path
from typing import Any

from jsonschema_rs import Draft202012Validator

from bancaemdia.api.contracts import ErrorResponse
from bancaemdia.main import app


def test_hypothetical_contract_has_exact_money_auth_explicit_read_policy_and_examples() -> None:
    document = app.openapi()
    operation = document["paths"]["/api/v1/painel/hipotetica"]["get"]
    assert operation["security"]
    assert operation["x-bancaemdia-policy"]["access_modes"] == ["FULL_WRITE", "READ_ONLY"]
    assert "máximo 10000" in operation["description"]
    assert "402" not in operation["responses"]
    assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    schemas = document["components"]["schemas"]
    for name in ["CenarioHipotetica", "DiferencasHipotetica"]:
        for key, field in schemas[name]["properties"].items():
            if key.endswith("_centavos"):
                money = field["anyOf"][0]
                if "$ref" in money:
                    money = schemas[money["$ref"].rsplit("/", 1)[1]]
                assert money["type"] == "string"
                assert "pattern" in money
                assert {"type": "null"} in field["anyOf"]
    examples: dict[str, Any] = json.loads(
        (Path(__file__).parents[2] / "docs/contracts/examples/hipotetica.json").read_text()
    )
    validator = Draft202012Validator({
        **schemas["HipoteticaSaida"],
        "components": document["components"],
    })
    for key in ("comparado", "vazio", "indisponivel"):
        validator.validate(examples[key])
    for example in examples["erros"].values():
        ErrorResponse.model_validate(example)
    assert examples["comparado"]["hipotetico"]["lucro_centavos"] == "2000"
    assert examples["indisponivel"]["hipotetico"]["lucro_centavos"] is None
