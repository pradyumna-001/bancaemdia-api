"""API N/N-1 compatibility; this does not claim to test the extension's outbox."""

from bancaemdia.api.collection_contract import CONTRACT_PATHS, artifact, release
from bancaemdia.api.openapi import REQUEST_EXAMPLES
from bancaemdia.domain.coleta_provenance import content_hash, safe_payload, source_times
from bancaemdia.schemas.coleta_v2 import CollectionBatch
from bancaemdia.workers.celery_app import app


def test_v1_v2_are_published_together_and_breaking_versions_are_not_implicitly_accepted():
    document = artifact()
    assert set(document["paths"]) == CONTRACT_PATHS
    v1 = document["paths"]["/api/v1/coleta"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    assert v1["properties"]["contrato"]["const"] == 1
    v2 = document["components"]["schemas"]["CollectionBatch"]
    assert v2["properties"]["contrato"]["const"] == 2
    assert release().client_protocol_min == 1 and release().client_protocol_max == 2
    example = REQUEST_EXAMPLES["post", "/api/v1/coleta/batches"][1]
    parsed = CollectionBatch.model_validate(example)
    assert parsed.items[0].content_hash == content_hash(parsed.items[0].payload)


def test_shared_inbox_is_registered_for_recovery_without_overwriting_other_schedules():
    assert "bancaemdia.workers.coleta_v2" in app.conf.include
    assert app.conf.beat_schedule["collection-v2-inbox"]["task"] == "materialization.collection_v2"
    assert app.conf.beat_schedule["collection-v2-inbox"]["schedule"] == 10


def test_trust_boundary_rejects_credentials_and_does_not_invent_source_time():
    for payload in (
        {"cookie": "synthetic"},
        {"nested": {"access_token": "synthetic"}},
        {"unknown": "cti_SENTINEL"},
        {"secret": "synthetic"},
        {"invalid\x00key": "synthetic"},
    ):
        assert not safe_payload(payload)
    assert source_times("betano", {"capturado_em": "2026-08-02T00:00:00Z"}) == (None, None)
    assert content_hash({"a": "ação", "b": [1, True, None]}) == content_hash({
        "b": [1, True, None],
        "a": "ação",
    })
