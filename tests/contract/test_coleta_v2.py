import copy
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema_rs import Draft202012Validator
from pydantic import ValidationError

from bancaemdia.api.collection_contract import artifact, release, serialized
from bancaemdia.main import app
from bancaemdia.schemas.coleta_v2 import CollectionBatch

ROOT = Path(__file__).parents[2]


def batch():
    return {
        "contrato": 2,
        "batch_id": str(uuid4()),
        "sessao_id": str(uuid4()),
        "items": [
            {
                "client_event_id": str(uuid4()),
                "hostname": "betano.bet.br",
                "observado": {
                    "source": "observed_response",
                    "transport": "fetch",
                    "method": "GET",
                    "path": "/synthetic/history",
                    "status": 200,
                    "content_type": "application/json",
                    "adapter_version": "1",
                    "sanitization_version": 1,
                },
                "capturado_em": "2026-08-02T00:00:00Z",
                "payload": {},
                "content_hash": hashlib.sha256(b"{}").hexdigest(),
                "conta_casa_ref": None,
            }
        ],
    }


def validator():
    document = artifact()
    schema = {"$ref": "#/components/schemas/CollectionBatch", "components": document["components"]}
    return Draft202012Validator(schema, validate_formats=True)


def test_published_artifact_and_metadata_match_runtime_byte_for_byte():
    document = artifact()
    assert (ROOT / "openapi/extension-collection-v2.json").read_bytes() == serialized(document)
    metadata = release().model_dump()
    assert (ROOT / "openapi/extension-collection-release.json").read_bytes() == serialized(metadata)
    assert metadata["artifact_sha256"] == hashlib.sha256(serialized(document)).hexdigest()
    assert metadata["supported_contracts"] == [1, 2] and metadata["deprecation_date"] is None
    client = TestClient(app, base_url="https://api.test")
    assert client.get("/api/v1/coleta/contract/schema").content == serialized(document)
    assert client.get("/api/v1/coleta/contract").json() == metadata
    assert "/api/v1/coleta" in document["paths"] and "/api/v1/coleta/batches" in document["paths"]


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "event_id",
        "no_session",
        "too_many",
        "transport",
        "timezone",
        "extra",
        "host",
        "empty",
    ],
)
def test_structural_rejections_match_published_schema_and_runtime_model(mutation):
    data = batch()
    item = data["items"][0]
    if mutation == "version":
        data["contrato"] = 3
    elif mutation == "event_id":
        item["client_event_id"] = "bad"
    elif mutation == "no_session":
        data.pop("sessao_id")
    elif mutation == "too_many":
        data["items"] = [copy.deepcopy(item) for _ in range(101)]
    elif mutation == "transport":
        item["observado"]["source"] = "automated_request"
    elif mutation == "timezone":
        item["capturado_em"] = "2026-08-02T00:00:00"
    elif mutation == "extra":
        item["usuario_id"] = 7
    elif mutation == "host":
        item["hostname"] = "https://betano.bet.br"
    elif mutation == "empty":
        data["items"] = []
    assert list(validator().iter_errors(data))
    with pytest.raises(ValidationError):
        CollectionBatch.model_validate(data)


def test_bounded_valid_envelope_matches_schema_and_model():
    data = batch()
    validator().validate(data)
    assert CollectionBatch.model_validate(data).contrato == 2


def test_secrets_and_oversized_transport_are_not_echoed():
    client = TestClient(app, base_url="https://api.test")
    # Body limit runs on streamed bytes before parsing, including without Content-Length.
    response = client.post(
        "/api/v1/coleta/batches",
        content=iter([b'{"padding":"', b"x" * (1024 * 1024), b'"}']),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    invalid = batch()
    invalid["items"][0]["observado"]["source"] = "cti_SENTINEL"
    response = client.post("/api/v1/coleta/batches", json=invalid)
    assert response.status_code in (401, 422) and "SENTINEL" not in response.text
    insecure = TestClient(app, base_url="http://api.test").post(
        "/api/v1/coleta/batches", json=batch()
    )
    assert insecure.status_code == 400 and "location" not in insecure.headers
