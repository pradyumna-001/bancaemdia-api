"""Temporary review-gate simulations; assertions never attest human fixture approval."""

import copy
import json
from types import SimpleNamespace

import pytest

from bancaemdia.coleta.readers.base import canonical_bytes, digest
from bancaemdia.coleta.readers.reference import reference_registry
from scripts import validate_reader_fixtures as scanner
from tests.coleta.harness import (
    FixtureContractError,
    bundle_digest,
    reviewed_manifest,
    run_fixture_set,
)
from tests.coleta.test_reader_contract import envelope, source


def write_json(path, value):
    path.write_bytes(canonical_bytes(value))


def temporary_bundle(directory):
    directory.mkdir(parents=True)
    raw = source(status="EXOTIC")
    values = {
        "probe.raw.json": raw,
        "probe.envelope.json": envelope(raw),
        "probe.golden.json": {"error_code": "unsupported_market", "reason": "market_not_supported"},
    }
    for name, value in values.items():
        write_json(directory / name, value)
    manifest = {
        "fixture_set_version": "1.0.0",
        "reader_id": "synthetic_reference",
        "reader_version": "1.0.0",
        "raw_schema_version": "example-1",
        "brand": "example",
        "hostname": "reader.example.invalid",
        "evidence_kind": "synthetic",
        "last_real_capture_at": None,
        "fixtures": [
            {
                "name": "probe",
                "covered_state": "UNSUPPORTED",
                "files": {name: digest((directory / name).read_bytes()) for name in values},
            }
        ],
    }
    manifest["review"] = {
        "status": "approved",
        "reviewer": "pradyumna-001",
        "reference": "https://github.com/pradyumna-001/bancaemdia-api/issues/114#issuecomment-1",
        "bundle_sha256": bundle_digest(manifest),
    }
    write_json(directory / "manifest.json", manifest)
    return manifest


def test_deterministic_independent_golden_and_honest_capability(tmp_path):
    directory = tmp_path / "temporary"
    temporary_bundle(directory)
    first = run_fixture_set(directory, reference_registry())
    assert first == run_fixture_set(directory, reference_registry())
    assert first["passed"] and first["fixture_count"] == 1
    assert first["covered_states"] == ["UNSUPPORTED"]
    assert first["last_real_capture_at"] is None
    assert first["production_support_proven"] is False


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "wrong_digest",
        "missing_reviewer",
        "extra_file",
        "changed_raw",
        "path_traversal",
        "fake_real_date",
        "duplicate_name",
        "missing_raw",
    ],
)
def test_review_and_bundle_mutations_are_rejected(tmp_path, change):
    directory = tmp_path / "temporary"
    manifest = temporary_bundle(directory)
    if change == "pending":
        manifest["review"]["status"] = "pending"
    elif change == "wrong_digest":
        manifest["review"]["bundle_sha256"] = "0" * 64
    elif change == "missing_reviewer":
        manifest["review"]["reviewer"] = None
    elif change == "extra_file":
        write_json(directory / "unlisted.json", {})
    elif change == "changed_raw":
        write_json(directory / "probe.raw.json", {"changed": True})
    elif change == "path_traversal":
        manifest["fixtures"][0]["name"] = "../probe"
    elif change == "fake_real_date":
        manifest["last_real_capture_at"] = "2026-01-01T00:00:00Z"
    elif change == "duplicate_name":
        manifest["fixtures"].append(copy.deepcopy(manifest["fixtures"][0]))
    else:
        del manifest["fixtures"][0]["files"]["probe.raw.json"]
    if change not in {"pending", "wrong_digest", "missing_reviewer"}:
        manifest["review"]["bundle_sha256"] = bundle_digest(manifest)
    write_json(directory / "manifest.json", manifest)
    with pytest.raises(FixtureContractError):
        reviewed_manifest(directory)


def test_even_simulated_reapproval_cannot_hide_golden_or_envelope_drift(tmp_path):
    directory = tmp_path / "temporary"
    manifest = temporary_bundle(directory)
    write_json(
        directory / "probe.golden.json",
        {"error_code": "schema_drift", "reason": "unrecognized_schema"},
    )
    manifest["fixtures"][0]["files"]["probe.golden.json"] = digest(
        (directory / "probe.golden.json").read_bytes()
    )
    manifest["review"]["bundle_sha256"] = bundle_digest(manifest)
    write_json(directory / "manifest.json", manifest)
    report = run_fixture_set(directory, reference_registry())
    assert not report["passed"] and report["drift_reason"] == "golden_mismatch"


def test_no_approved_set_is_reported_as_missing_not_passed_production(tmp_path):
    report = scanner.validate(root=tmp_path)
    assert report["reviewed_fixture_sets"] == 0
    assert report["fixture_gate"] == "no_human_reviewed_fixture_sets"
    assert report["production_registration_count"] == 0
    assert len(report["unmigrated_brand_readers"]) == 6
    assert all(not item["production_support_proven"] for item in report["unmigrated_brand_readers"])


def test_unreviewed_production_registration_fails_ci(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "DEFAULT_REGISTRY", reference_registry())
    with pytest.raises(ValueError, match="production registration requires"):
        scanner.validate(root=tmp_path)


def test_unlisted_sets_and_legacy_secrets_fail_scan(tmp_path):
    write_json(tmp_path / "legacy.json", {"nested": {"Authorization": "synthetic"}})
    with pytest.raises(ValueError):
        scanner.validate(root=tmp_path)
    (tmp_path / "legacy.json").unlink()
    (tmp_path / "reader-contract" / "unlisted").mkdir(parents=True)
    with pytest.raises(ValueError, match="unlisted"):
        scanner.validate(root=tmp_path)


@pytest.mark.parametrize(
    "change", [None, "bot", "different_author", "automation_account", "untrusted", "different_hash"]
)
def test_human_gate_uses_trusted_github_identity_and_exact_digest(tmp_path, monkeypatch, change):
    manifest = temporary_bundle(tmp_path / "temporary")
    comment = {
        "user": {"type": "User", "login": "pradyumna-001"},
        "author_association": "OWNER",
        "body": "Approved reader fixture bundle SHA256: " + manifest["review"]["bundle_sha256"],
    }
    if change == "bot":
        comment["user"]["type"] = "Bot"
    elif change == "different_author":
        comment["user"]["login"] = "other"
    elif change == "automation_account":
        comment["user"]["login"] = "wfcgit-hub"
        manifest["review"]["reviewer"] = "wfcgit-hub"
    elif change == "untrusted":
        comment["author_association"] = "NONE"
    elif change == "different_hash":
        comment["body"] = "Approved reader fixture bundle SHA256: " + "0" * 64
    monkeypatch.setattr(
        scanner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps(comment))
    )
    if change:
        with pytest.raises(ValueError, match="human approval"):
            scanner.verify_human_review(manifest["review"])
    else:
        scanner.verify_human_review(manifest["review"])
    manifest["review"]["reference"] = "https://other.invalid/comment"
    with pytest.raises(ValueError, match="GitHub"):
        scanner.verify_human_review(manifest["review"])


def test_version_bump_and_review_gate_run_before_reader(tmp_path, monkeypatch):
    directory = tmp_path / "reader-contract" / "temporary"
    manifest = temporary_bundle(directory)
    monkeypatch.setattr(scanner, "verify_human_review", lambda review: None)
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    previous = copy.deepcopy(manifest)
    previous["fixtures"][0]["files"]["probe.raw.json"] = "0" * 64
    monkeypatch.setattr(
        scanner.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(previous)),
    )
    with pytest.raises(ValueError, match="version bump"):
        scanner.validate("baseline", root=tmp_path, registry=reference_registry())
    manifest["fixture_set_version"] = "1.0.1"
    manifest["review"]["bundle_sha256"] = bundle_digest(manifest)
    write_json(directory / "manifest.json", manifest)
    assert (
        scanner.validate("baseline", root=tmp_path, registry=reference_registry())[
            "reviewed_fixture_sets"
        ]
        == 1
    )
