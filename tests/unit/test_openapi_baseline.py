import hashlib
import json

import pytest

from scripts import prepare_openapi_baseline as baseline


def test_valid_baseline_retains_all_contract_fields() -> None:
    document = {
        "openapi": "3.1.0",
        "paths": {"/accepted": {"post": {"responses": {"201": {"description": "Created"}}}}},
        "components": {"schemas": {"Accepted": {"type": "string", "enum": ["original"]}}},
    }
    assert baseline.parse_baseline(json.dumps(document).encode()) == document


def test_unknown_corruption_and_duplicate_fields_fail_closed() -> None:
    with pytest.raises(json.JSONDecodeError):
        baseline.parse_baseline(b'{"openapi":"3.1.0","paths":{} broken}')
    with pytest.raises(ValueError, match="Duplicate OpenAPI key"):
        baseline.parse_baseline(b'{"openapi":"3.1.0","paths":{},"paths":{"/lost":{}}}')


def test_exact_syntax_repair_preserves_schemas_required_fields_and_operations(monkeypatch) -> None:
    raw = b"""{
      "openapi": "3.1.0",
      "components": {"schemas": {"MatrizTitularSaida": {
        "properties": {"titular": {"type": "integer"}},
        "required": ["titular"],
        "title": "MatrizTitularSaida",
        "type": "object"
      }
        "type": "object"
      }, "Other": {"type": "boolean"}}},
      "paths": {"/accepted": {"get": {"responses": {"200": {"description": "OK"}}}}}
    }"""
    monkeypatch.setattr(baseline, "MAIN_SNAPSHOT_SHA256", hashlib.sha256(raw).hexdigest())
    document = baseline.parse_baseline(raw)
    assert {
        k: v
        for k, v in document["components"]["schemas"].items()
        if k in {"MatrizTitularSaida", "Other"}
    } == {
        "MatrizTitularSaida": {
            "properties": {"titular": {"type": "integer"}},
            "required": ["titular"],
            "title": "MatrizTitularSaida",
            "type": "object",
        },
        "Other": {"type": "boolean"},
    }
    assert document["paths"] == {
        "/accepted": {"get": {"responses": {"200": {"description": "OK"}}}}
    }


def test_repair_does_not_accept_other_bytes_even_with_same_syntax() -> None:
    with pytest.raises(json.JSONDecodeError):
        baseline.parse_baseline(b'{"openapi":"3.1.0","paths":{}} trailing')


def test_unknown_baseline_with_missing_definition_fails_closed() -> None:
    document = {
        "openapi": "3.1.0",
        "paths": {},
        "components": {"schemas": {"Accepted": {"$ref": "#/components/schemas/JsonValue"}}},
    }
    with pytest.raises(ValueError, match="Unresolved OpenAPI reference"):
        baseline.parse_baseline(json.dumps(document).encode())


def test_restored_definitions_retain_the_accepted_constraints() -> None:
    schemas = baseline._RECOVERED_SCHEMAS
    assert set(schemas) == {"JsonValue", "ItemErroValidacaoSaida", "LivenessResponse"}
    assert schemas["JsonValue"]["oneOf"] == [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "array", "items": {"$ref": "#/components/schemas/JsonValue"}},
        {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/JsonValue"}},
        {"type": "null"},
    ]
    validation = schemas["ItemErroValidacaoSaida"]
    assert validation["additionalProperties"] is False
    assert validation["required"] == ["loc", "msg", "type"]
    assert validation["properties"]["input"] == {"$ref": "#/components/schemas/JsonValue"}
    liveness = schemas["LivenessResponse"]
    assert liveness["additionalProperties"] is False
    assert liveness["required"] == ["status"]
    assert liveness["properties"]["status"]["const"] == "ok"
