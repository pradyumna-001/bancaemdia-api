import json
import re
from pathlib import Path

from bancaemdia.middleware.compression import COMPRESSIBLE_READS

SCHEMA = json.loads(
    (Path(__file__).parent / "schemas" / "openapi.json").read_text(encoding="utf-8")
)
# Nome de campo que denunciaria segredo de autenticação misturado ao corpo comprimido (BREACH).
SECRET_FIELD = re.compile(
    r"token|csrf|secret|segredo|senha|password|credencial|credential|cookie|sess(ao|ion)|codigo|code|api_key",
    re.IGNORECASE,
)


def _fields(schema, seen=None):
    seen = set() if seen is None else seen
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return set()
        seen.add(name)
        return _fields(SCHEMA["components"]["schemas"][name], seen)
    found = set()
    for key in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(key, []):
            found |= _fields(option, seen)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            found |= _fields(schema[key], seen)
    for name, value in schema.get("properties", {}).items():
        found |= {name} | _fields(value, seen)
    return found


def test_published_compressible_reads_are_json_only_and_carry_no_secret_field():
    published = {path for path in COMPRESSIBLE_READS if path in SCHEMA["paths"]}
    # As leituras da main; fila e filtros entram neste teste quando seus PRs forem integrados.
    assert {"/api/v1/apostas", "/api/v1/painel", "/api/v1/painel/analises"} <= published
    for path in published:
        success = {
            status: response
            for status, response in SCHEMA["paths"][path]["get"]["responses"].items()
            if status.startswith("2")
        }
        assert set(success) == {"200"}, path
        content = success["200"]["content"]
        assert set(content) == {"application/json"}, path
        leaked = {
            name
            for name in _fields(content["application/json"]["schema"])
            if SECRET_FIELD.search(name)
        }
        assert not leaked, (path, leaked)
