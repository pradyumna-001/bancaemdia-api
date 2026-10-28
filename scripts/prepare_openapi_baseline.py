"""Read the actual main contract; repair identified defects in one accepted merge.

The accepted 2026-10-06 merge duplicated the closing type/object fragment of
MatrizTitularSaida. Its exact byte digest is allowlisted so this repair cannot
silently accept another malformed baseline. No operation, property, requirement
or response is removed, and oasdiff still compares every accepted contract field.
The same accepted merge also deleted three referenced definitions. They are
restored verbatim from its accepted parent 00ae1e0b1d5d89224ce5028447767a23f527ad6e.
No definition from the new PR is used as a comparison baseline. Other snapshots
are left untouched and unresolved component references fail closed.

The 2026-10-27 rebase of the merged PRs left two conflict blocks in the main
snapshot whose HEAD side is empty; the other side holds the definitions those
accepted PRs published. Only that exact digest has its marker lines dropped.
The same rebase deleted ManualAssignmentNotice; it is restored verbatim from
the accepted head of #203 (bc368106da04a24c8a8b9a9827978ce476963e19).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

MAIN_SNAPSHOT_SHA256 = "2d5d9b20cc227a356a26c0569ef0af47146fedd1c7e358fc3fae0630e5172a2f"
REBASE_SNAPSHOT_SHA256 = "d09fa6c244f942955bef7587ac73a622150d737350d1432719f7c939b90b85a0"
REBASE_CONFLICT_BLOCKS = 2
_DUPLICATED_END = """        "title": "MatrizTitularSaida",
        "type": "object"
      }
        "type": "object"
      },"""
_CORRECT_END = """        "title": "MatrizTitularSaida",
        "type": "object"
      },"""


# Verbatim definitions from the immutable accepted parent listed above.
_RECOVERED_SCHEMAS: dict[str, Any] = {
    "JsonValue": {
        "description": "A recursively typed JSON value; never an unconstrained Any schema.",
        "oneOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"items": {"$ref": "#/components/schemas/JsonValue"}, "type": "array"},
            {"additionalProperties": {"$ref": "#/components/schemas/JsonValue"}, "type": "object"},
            {"type": "null"},
        ],
        "title": "JsonValue",
    },
    "ItemErroValidacaoSaida": {
        "additionalProperties": False,
        "properties": {
            "ctx": {
                "anyOf": [
                    {
                        "additionalProperties": {"$ref": "#/components/schemas/JsonValue"},
                        "type": "object",
                    },
                    {"type": "null"},
                ],
                "title": "Ctx",
            },
            "input": {"$ref": "#/components/schemas/JsonValue"},
            "loc": {
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "title": "Loc",
                "type": "array",
            },
            "msg": {"title": "Msg", "type": "string"},
            "type": {"title": "Type", "type": "string"},
        },
        "required": ["loc", "msg", "type"],
        "title": "ItemErroValidacaoSaida",
        "type": "object",
    },
    "LivenessResponse": {
        "additionalProperties": False,
        "properties": {"status": {"const": "ok", "title": "Status", "type": "string"}},
        "required": ["status"],
        "title": "LivenessResponse",
        "type": "object",
    },
}


_REBASE_RECOVERED_SCHEMAS: dict[str, Any] = {
    "ManualAssignmentNotice": {
        "additionalProperties": False,
        "properties": {
            "codigo": {
                "enum": ["conta_ausente", "conta_ambigua"],
                "title": "Codigo",
                "type": "string",
            },
            "encaminhamento": {
                "const": "revisar_conta",
                "default": "revisar_conta",
                "title": "Encaminhamento",
                "type": "string",
            },
            "revisao_id": {
                "pattern": "^[1-9][0-9]{0,18}$",
                "title": "Revisao Id",
                "type": "string",
            },
        },
        "required": ["codigo", "revisao_id"],
        "title": "ManualAssignmentNotice",
        "type": "object",
    },
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate OpenAPI key: {key}")
        result[key] = value
    return result


def _drop_empty_head_conflicts(text: str) -> str:
    lines = text.split("\n")
    kept: list[str] = []
    blocks = 0
    index = 0
    while index < len(lines):
        if lines[index] != "<<<<<<< HEAD":
            kept.append(lines[index])
            index += 1
            continue
        end = next(
            (
                position
                for position in range(index + 2, len(lines))
                if lines[position].startswith(">>>>>>> ")
            ),
            None,
        )
        accepted = lines[index + 2 : end]
        if (
            index + 1 >= len(lines)
            or lines[index + 1] != "======="
            or end is None
            or any(line.startswith(("<<<<<<<", "=======", "|||||||")) for line in accepted)
        ):
            raise ValueError("Known rebase snapshot no longer matches the conflict repair")
        kept.extend(accepted)
        blocks += 1
        index = end + 1
    if blocks != REBASE_CONFLICT_BLOCKS:
        raise ValueError("Known rebase snapshot no longer matches the conflict repair")
    return "\n".join(kept)


def parse_baseline(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if digest == REBASE_SNAPSHOT_SHA256:
        text = _drop_empty_head_conflicts(text)
        logging.warning("Dropped the known rebase conflict markers; full oasdiff remains required")
    known_merge = digest == MAIN_SNAPSHOT_SHA256
    try:
        document = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        if not known_merge:
            raise
        if text.count(_DUPLICATED_END) != 1:
            raise ValueError(
                "Known main snapshot no longer matches the precise syntax repair"
            ) from error
        document = json.loads(
            text.replace(_DUPLICATED_END, _CORRECT_END, 1), object_pairs_hook=_unique_object
        )
        logging.warning(
            "Repaired known main JSON syntax duplication; full oasdiff remains required"
        )
    if not isinstance(document, dict) or "openapi" not in document or "paths" not in document:
        raise ValueError("Baseline is not an OpenAPI document")
    if known_merge:
        schemas = document.setdefault("components", {}).setdefault("schemas", {})
        for name, definition in _RECOVERED_SCHEMAS.items():
            if name in schemas:
                raise ValueError(f"Known merge unexpectedly contains restored schema: {name}")
            schemas[name] = definition
        logging.warning("Restored three deleted definitions from the accepted parent")
    if digest == REBASE_SNAPSHOT_SHA256:
        schemas = document.setdefault("components", {}).setdefault("schemas", {})
        for name, definition in _REBASE_RECOVERED_SCHEMAS.items():
            if name in schemas:
                raise ValueError(f"Known rebase unexpectedly contains restored schema: {name}")
            schemas[name] = definition
        logging.warning("Restored the definition deleted by the known rebase")
    _validate_references(document)
    return document


def _validate_references(document: dict[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/"):
                target: Any = document
                try:
                    for component in reference[2:].split("/"):
                        target = target[component.replace("~1", "/").replace("~0", "~")]
                except (KeyError, TypeError) as error:
                    raise ValueError(f"Unresolved OpenAPI reference: {reference}") from error
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(document)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = subprocess.check_output([
        "git",
        "show",
        f"{args.ref}:tests/contract/schemas/openapi.json",
    ])
    document = parse_baseline(raw)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
