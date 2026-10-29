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


_REBASE_RAW = b"""{
  "openapi": "3.1.0",
  "components": {"schemas": {
    "Before": {"type": "string"},
<<<<<<< HEAD
=======
    "Published": {"type": "integer"},
>>>>>>> d0523b7 (fix(manual): accepted side)
    "Middle": {"type": "string"},
<<<<<<< HEAD
=======
    "Evidence": {"type": "boolean"},
>>>>>>> e2b7f17 (fix(revisao): accepted side)
    "After": {"type": "string"},
    "MetaEntrada": {"properties": {
      "alvo": {"anyOf": [{"type": "number"}, {"type": "string"}]},
      "linha_base": {"anyOf": [{"type": "string"}, {"type": "null"}]}
    }},
    "MetaAlteracao": {"properties": {
      "alvo": {"anyOf": [{"type": "string"}, {"type": "null"}]},
      "linha_base": {"anyOf": [{"type": "string"}, {"type": "null"}]}
    }}
  }},
  "paths": {"/accepted": {"get": {"responses": {"200": {"description": "OK"}}}}}
}"""


def test_known_rebase_conflicts_keep_every_accepted_definition(monkeypatch) -> None:
    digest = hashlib.sha256(_REBASE_RAW).hexdigest()
    monkeypatch.setattr(baseline, "REBASE_SNAPSHOT_SHA256", digest)
    document = baseline.parse_baseline(_REBASE_RAW)
    schemas = document["components"]["schemas"]
    assert {name: schemas[name] for name in ("Before", "Published", "Middle", "Evidence")} == {
        "Before": {"type": "string"},
        "Published": {"type": "integer"},
        "Middle": {"type": "string"},
        "Evidence": {"type": "boolean"},
    }
    assert (
        schemas["ManualAssignmentNotice"]
        == baseline._REBASE_RECOVERED_SCHEMAS["ManualAssignmentNotice"]
    )
    for name, field in baseline._GOAL_DECIMAL_FIELDS:
        texts = [v for v in schemas[name]["properties"][field]["anyOf"] if v["type"] == "string"]
        assert texts == [{"type": "string", "pattern": baseline._GOAL_DECIMAL_PATTERN}]
    assert schemas["MetaEntrada"]["properties"]["alvo"]["anyOf"][0] == {"type": "number"}
    notice = baseline._REBASE_RECOVERED_SCHEMAS["ManualAssignmentNotice"]
    assert notice["required"] == ["codigo", "revisao_id"]
    assert notice["additionalProperties"] is False


def test_rebase_repair_refuses_other_bytes_and_changed_conflicts(monkeypatch) -> None:
    with pytest.raises(json.JSONDecodeError):
        baseline.parse_baseline(_REBASE_RAW)
    changed = _REBASE_RAW.replace(b'=======\n    "Published"', b'    "Published"')
    monkeypatch.setattr(baseline, "REBASE_SNAPSHOT_SHA256", hashlib.sha256(changed).hexdigest())
    with pytest.raises(ValueError, match="no longer matches the conflict repair"):
        baseline.parse_baseline(changed)


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
