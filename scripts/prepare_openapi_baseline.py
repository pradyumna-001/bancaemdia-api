"""Read the actual main contract; repair one identified merge syntax defect.

The accepted 2026-10-06 merge duplicated the closing type/object fragment of
MatrizTitularSaida. Its exact byte digest is allowlisted so this repair cannot
silently accept another malformed baseline. No operation, property, requirement
or response is removed, and oasdiff still compares every accepted contract field.
The normal path uses the untouched snapshot as soon as main contains valid JSON.
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
_DUPLICATED_END = """        "title": "MatrizTitularSaida",
        "type": "object"
      }
        "type": "object"
      },"""
_CORRECT_END = """        "title": "MatrizTitularSaida",
        "type": "object"
      },"""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate OpenAPI key: {key}")
        result[key] = value
    return result


def parse_baseline(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8")
    try:
        document = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        if hashlib.sha256(raw).hexdigest() != MAIN_SNAPSHOT_SHA256:
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
    return document


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
