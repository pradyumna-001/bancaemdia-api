"""The complete application publishes binary photo delivery, never a hash lookup."""

import json
from pathlib import Path
from typing import Any

from bancaemdia.api.contracts import ErrorResponse
from bancaemdia.api.v1.aposta_foto import INVALID, NOT_FOUND, UNAVAILABLE
from bancaemdia.domain.access import READ_OPERATION_MODES
from bancaemdia.domain.billing import AccessMode
from bancaemdia.domain.foto_aposta import IMAGE_TYPES
from bancaemdia.main import app

ROOT = Path(__file__).resolve().parents[2]


def test_photo_binary_contract_and_examples_match_actual_app() -> None:
    schema: dict[str, Any] = app.openapi()
    assert schema == json.loads(
        (ROOT / "tests/contract/schemas/openapi.json").read_text(encoding="utf-8")
    )
    operation = schema["paths"]["/api/v1/apostas/{chave}/foto"]["get"]
    parameters = operation["parameters"]
    assert len(parameters) == 1
    assert parameters[0]["name"] == "chave" and parameters[0]["in"] == "path"
    assert parameters[0]["required"]
    assert parameters[0]["schema"]["maxLength"] == 256
    assert "requestBody" not in operation
    responses = operation["responses"]
    assert "402" not in responses
    assert {"200", "401", "404", "422", "429", "500", "503"} <= responses.keys()
    assert set(responses["200"]["content"]) == set(IMAGE_TYPES.values())
    for mime in IMAGE_TYPES.values():
        assert responses["200"]["content"][mime]["schema"] == {"type": "string", "format": "binary"}
    for status in ("429", "503"):
        assert responses[status]["headers"]["Retry-After"]["schema"]["type"] == "integer"
    assert READ_OPERATION_MODES["aposta_foto"] == {AccessMode.FULL_WRITE, AccessMode.READ_ONLY}
    example = json.loads(
        (ROOT / "docs/contracts/examples/aposta-foto.json").read_text(encoding="utf-8")
    )
    assert example["success"]["headers"]["Cache-Control"] == "private, no-store"
    bodies = {}
    for error in example["errors"]:
        ErrorResponse.model_validate(error["body"])
        assert str(error["status"]) in responses
        bodies[error["status"]] = error["body"]
    assert bodies[404] == {"detail": NOT_FOUND}
    assert bodies[422] == {"detail": INVALID}
    assert bodies[503] == {"detail": UNAVAILABLE}
