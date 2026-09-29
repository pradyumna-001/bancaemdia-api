"""Reproducible publication built from exactly the schemas used by the runtime."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel

CONTRACT_PATHS = {
    "/coleta",
    "/api/v1/coleta",
    "/api/v1/coleta/sessions",
    "/api/v1/coleta/sessions/{sessao_id}",
    "/api/v1/coleta/batches",
    "/api/v1/coleta/jobs/{job_id}",
}


class CollectionRelease(BaseModel):
    current_contract: int
    supported_contracts: list[int]
    client_protocol_min: int
    client_protocol_max: int
    schema_sha256: str
    artifact_sha256: str
    artifact: str
    deprecation_date: str | None
    minimum_deprecation_days: int


def artifact() -> dict[str, Any]:
    from bancaemdia.main import app

    document = app.openapi()
    paths = {path: value for path, value in document["paths"].items() if path in CONTRACT_PATHS}
    definitions = document["components"]["schemas"]
    needed: set[str] = set()

    def references(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in needed:
                    needed.add(name)
                    references(definitions[name])
            for child in value.values():
                references(child)
        elif isinstance(value, list):
            for child in value:
                references(child)

    references(paths)
    return {
        "openapi": document["openapi"],
        "info": {"title": "Bancaemdia Collection", "version": "2.0.0"},
        "paths": paths,
        "components": {
            "schemas": {name: definitions[name] for name in sorted(needed)},
            "securitySchemes": {
                "CollectionToken": document["components"]["securitySchemes"]["CollectionToken"]
            },
        },
    }


def serialized(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def release() -> CollectionRelease:
    document = artifact()
    return CollectionRelease(
        current_contract=2,
        supported_contracts=[1, 2],
        client_protocol_min=1,
        client_protocol_max=2,
        schema_sha256=hashlib.sha256(serialized(document["components"]["schemas"])).hexdigest(),
        artifact_sha256=hashlib.sha256(serialized(document)).hexdigest(),
        artifact="/api/v1/coleta/contract/schema",
        deprecation_date=None,
        minimum_deprecation_days=90,
    )
