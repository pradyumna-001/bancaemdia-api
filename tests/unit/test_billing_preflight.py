import copy
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bancaemdia.domain.billing_preflight import CHECKS, MAX_MANIFEST_BYTES, assess_preflight

TODAY = date(2026, 10, 6)
ROOT = Path(__file__).resolve().parents[2]


def complete_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "seller_country": "BR",
        "account": {
            "mode": "production",
            "charges_enabled": True,
            "payouts_enabled": True,
            "details_submitted": True,
            "requirements_due_count": 0,
            "requirements_pending_count": 0,
        },
        "checks": {
            name: {
                "status": "verified",
                "evidence_sha256": "a" * 64,
                "reviewed_on": TODAY.isoformat(),
                "reviewer_role": "fiscal_adviser" if name.startswith("tax_") else "owner",
            }
            for name in CHECKS
        },
    }


def test_even_a_complete_attestation_never_authorizes_production() -> None:
    original = complete_manifest()
    manifest = copy.deepcopy(original)
    report = assess_preflight(json.dumps(manifest).encode(), today=TODAY)
    assert report.state == "OWNER_REVIEW_REQUIRED"
    assert report.blocking_checks == []
    assert report.production_authorized is False
    assert manifest == original


@pytest.mark.parametrize("missing", CHECKS)
def test_every_missing_gate_remains_a_blocker(missing: str) -> None:
    manifest = complete_manifest()
    del manifest["checks"][missing]
    report = assess_preflight(json.dumps(manifest).encode(), today=TODAY)
    assert report.state == "INCOMPLETE"
    assert report.blocking_checks == [missing]


@pytest.mark.parametrize("days", [-1, 31])
def test_future_or_stale_account_evidence_is_not_current(days: int) -> None:
    manifest = complete_manifest()
    manifest["checks"]["account_verification"]["reviewed_on"] = (
        TODAY - timedelta(days=days)
    ).isoformat()
    report = assess_preflight(json.dumps(manifest).encode(), today=TODAY)
    assert report.blocking_checks == ["account_verification"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "sandbox"),
        ("charges_enabled", False),
        ("payouts_enabled", False),
        ("details_submitted", False),
        ("requirements_due_count", 1),
        ("requirements_pending_count", 1),
        ("requirements_due_count", None),
    ],
)
def test_sandbox_and_unverified_account_capabilities_cannot_certify_launch(
    field: str, value: object
) -> None:
    manifest = complete_manifest()
    manifest["account"][field] = value
    report = assess_preflight(json.dumps(manifest).encode(), today=TODAY)
    assert report.blocking_checks == ["account_verification"]
    assert report.production_authorized is False


@pytest.mark.parametrize("value", ["true", 1])
def test_string_or_numeric_booleans_are_rejected(value: object) -> None:
    manifest = complete_manifest()
    manifest["account"]["charges_enabled"] = value
    report = assess_preflight(json.dumps(manifest).encode(), today=TODAY)
    assert report.state == "INVALID_INPUT"


@pytest.mark.parametrize("role", ["agent", "owner", "account_operator"])
def test_fiscal_attestation_requires_the_fiscal_role(role: str) -> None:
    manifest = complete_manifest()
    manifest["checks"]["tax_reporting_and_invoicing"]["reviewer_role"] = role
    report = assess_preflight(json.dumps(manifest).encode(), today=TODAY)
    if role == "agent":
        assert report.state == "INVALID_INPUT"
    else:
        assert report.blocking_checks == ["tax_reporting_and_invoicing"]


def test_declaring_verified_without_a_document_hash_does_not_clear_a_gate() -> None:
    manifest = complete_manifest()
    del manifest["checks"]["commercial_catalog"]["evidence_sha256"]
    assert assess_preflight(json.dumps(manifest).encode(), today=TODAY).blocking_checks == [
        "commercial_catalog"
    ]


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b"x" * (MAX_MANIFEST_BYTES + 1)])
def test_invalid_or_oversized_documents_fail_without_echoing_input(raw: bytes) -> None:
    report = assess_preflight(raw, today=TODAY)
    assert report.state == "INVALID_INPUT"
    assert report.blocking_checks == ["invalid_manifest"]


def test_template_reports_all_pending_gates() -> None:
    raw = (ROOT / "docs/validation/billing-preflight-template.json").read_bytes()
    report = assess_preflight(raw, today=TODAY)
    assert report.state == "INCOMPLETE"
    assert report.blocking_checks == list(CHECKS)


def test_thirty_day_boundary_is_accepted_but_still_does_not_authorize() -> None:
    manifest = complete_manifest()
    manifest["checks"]["account_verification"]["reviewed_on"] = (
        TODAY - timedelta(days=30)
    ).isoformat()
    report = assess_preflight(json.dumps(manifest).encode(), today=TODAY)
    assert report.blocking_checks == []
    assert report.production_authorized is False


@pytest.mark.parametrize("state", ["complete", "pending", "missing"])
def test_cli_reports_each_operational_exit_state(tmp_path: Path, state: str) -> None:
    path = tmp_path / "private.json"
    if state == "complete":
        manifest = complete_manifest()
        for check in manifest["checks"].values():
            check["reviewed_on"] = datetime.now(UTC).date().isoformat()
        path.write_text(json.dumps(manifest), encoding="utf-8")
    elif state == "pending":
        path.write_bytes((ROOT / "docs/validation/billing-preflight-template.json").read_bytes())
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_billing_preflight.py"), str(path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    expected = {
        "complete": (0, "OWNER_REVIEW_REQUIRED"),
        "pending": (1, "INCOMPLETE"),
        "missing": (2, "INVALID_INPUT"),
    }[state]
    report = json.loads(result.stdout)
    assert (result.returncode, report["state"]) == expected
    assert report["production_authorized"] is False
    assert result.stderr == ""
    assert str(path) not in result.stdout


def test_cli_rejects_private_extra_fields_without_printing_them(tmp_path: Path) -> None:
    manifest = complete_manifest()
    manifest["private_account_notes"] = "must-not-appear-in-output"
    path = tmp_path / "private.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_billing_preflight.py"), str(path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    assert "must-not-appear" not in result.stdout
    assert json.loads(result.stdout)["state"] == "INVALID_INPUT"
