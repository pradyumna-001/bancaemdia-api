"""Published independent goldens and the owner's narrowly scoped publication decision."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from bancaemdia.coleta.readers.reference import reference_registry
from scripts.validate_reader_fixtures import validate
from tests.coleta.harness import (
    FixtureContractError,
    GoldenReplay,
    bundle_digest,
    fixture_contents,
    reviewed_manifest,
    run_fixture_set,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/coleta"
BUNDLE = FIXTURES / "reader-contract/example-v1"


def test_all_published_goldens_and_capability_evidence():
    report = validate()
    assert report["published_fixture_sets"] == 1
    assert report["reviewed_fixture_sets"] == 0
    assert report["publication_authorized_fixture_sets"] == 1
    assert report["fixture_gate"] == "owner_authorized_synthetic_pending_admin_review"
    assert report["production_registration_count"] == 0
    result = report["integrations"][0]
    assert result["passed"] and result["fixture_count"] == 11
    assert result["covered_states"] == [
        "ANULADA",
        "CASHOUT",
        "GREEN",
        "MEIO_GREEN",
        "MEIO_RED",
        "PENDENTE",
        "RED",
        "UNSUPPORTED",
    ]
    assert result["last_real_capture_at"] is None
    assert result["production_support_proven"] is False
    assert result == run_fixture_set(BUNDLE, reference_registry())


@pytest.mark.parametrize(
    "name",
    [
        "open",
        "green",
        "red",
        "void",
        "cashout",
        "half-green",
        "half-red",
        "multiple",
        "brazilian",
        "optional-missing",
    ],
)
def test_published_fixture_replay_preserves_one_identity(name):
    envelope, _ = fixture_contents(BUNDLE, name)
    reader = reference_registry()
    replay = GoldenReplay({})
    assert replay.apply(reader.read(envelope)) == ["created"]
    envelope["captured_at"] = "2026-10-06T15:00:00Z"
    assert replay.apply(reader.read(envelope)) == ["noop"]
    assert len(replay.bets) == 1


def test_published_open_to_settled_update_uses_same_bet():
    reader = reference_registry()
    replay = GoldenReplay({})
    opened = reader.read(fixture_contents(BUNDLE, "open")[0])
    settled = reader.read(fixture_contents(BUNDLE, "green")[0])
    assert opened.bets[0].identity_hash == settled.bets[0].identity_hash
    assert opened.bets[0].canonical_hash != settled.bets[0].canonical_hash
    assert replay.apply(opened) == ["created"]
    assert replay.apply(settled) == ["updated"]
    assert replay.apply(settled) == ["noop"]
    assert replay.apply(opened) == ["stale_or_conflicting"]
    bet = next(iter(replay.bets.values()))
    assert len(replay.bets) == 1
    assert (bet.state, bet.stake_centavos, bet.return_centavos) == ("GREEN", 10000, 20000)


@pytest.mark.parametrize(
    "change", ["real", "other_host", "version", "reviewer", "reference", "raw"]
)
def test_publication_decision_cannot_authorize_other_evidence(tmp_path, change):
    target = tmp_path / "candidate"
    shutil.copytree(BUNDLE, target)
    manifest = copy.deepcopy(reviewed_manifest(target))
    if change == "real":
        manifest["evidence_kind"] = "sanitized_real"
        manifest["last_real_capture_at"] = "2026-10-06T15:00:00Z"
    elif change == "other_host":
        manifest["hostname"] = "other.example.invalid"
    elif change == "version":
        manifest["fixture_set_version"] = "1.0.1"
    elif change == "reviewer":
        manifest["review"]["reviewer"] = "pradyumna-001"
    elif change == "reference":
        manifest["review"]["reference"] = (
            "https://github.com/pradyumna-001/bancaemdia-api/issues/114#issuecomment-1"
        )
    else:
        (target / "open.raw.json").write_text("{}", encoding="utf-8")
    manifest["review"]["bundle_sha256"] = bundle_digest(manifest)
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FixtureContractError):
        reviewed_manifest(target)


def test_candidate_never_authorizes_production_registration(monkeypatch):
    from scripts import validate_reader_fixtures as scanner

    monkeypatch.setattr(scanner, "DEFAULT_REGISTRY", reference_registry())
    with pytest.raises(ValueError, match="production registration requires"):
        scanner.validate()
