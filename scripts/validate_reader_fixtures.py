"""Scan every capture fixture and verify reviewed hashes, versions and independent goldens."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bancaemdia.coleta.leitores import LEITORES  # ruff: ignore[module-import-not-at-top-of-file]
from bancaemdia.coleta.readers.base import decode_raw  # ruff: ignore[module-import-not-at-top-of-file]
from bancaemdia.coleta.readers.reference import reference_registry  # ruff: ignore[module-import-not-at-top-of-file]
from bancaemdia.coleta.readers.registry import DEFAULT_REGISTRY, ReaderRegistry  # ruff: ignore[module-import-not-at-top-of-file]
from tests.coleta.harness import (  # ruff: ignore[module-import-not-at-top-of-file]
    bundle_digest,
    owner_authorized_candidate,
    reviewed_manifest,
    run_fixture_set,
)

# The user requested administrator review. The automation account must never self-attest.
HUMAN_REVIEWERS = frozenset({"pradyumna-001"})


def stable_version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("stable fixture version required")
    return tuple(map(int, value.split(".")))


def verify_human_review(review: dict) -> None:
    """A manifest flag is insufficient: require the trusted human's exact GitHub attestation."""
    match = re.fullmatch(
        r"https://github\.com/pradyumna-001/bancaemdia-api/(?:issues|pull)/\d+#issuecomment-(\d+)",
        review["reference"],
    )
    if not match:
        raise ValueError("GitHub human review comment required")
    response = subprocess.run(
        ["gh", "api", f"repos/pradyumna-001/bancaemdia-api/issues/comments/{match[1]}"],
        check=True,
        capture_output=True,
        text=True,
    )
    comment = json.loads(response.stdout)
    if (
        comment["user"]["type"] != "User"
        or comment["user"]["login"] not in HUMAN_REVIEWERS
        or comment["user"]["login"] != review["reviewer"]
        or comment["author_association"] not in {"OWNER", "COLLABORATOR", "MEMBER"}
        or f"Approved reader fixture bundle SHA256: {review['bundle_sha256']}"
        not in comment["body"].splitlines()
    ):
        raise ValueError("trusted human approval of exact bundle missing")


def validate(
    base_ref: str | None = None,
    *,
    root: Path | None = None,
    registry: ReaderRegistry | None = None,
) -> dict:
    root = root or ROOT / "tests/fixtures/coleta"
    # Legacy samples are scanned, but cannot prove current exact-domain reader support.
    legacy = []
    for path in sorted(root.glob("*.json")):
        decode_raw(path.read_text(encoding="utf-8"))
        if base_ref:
            relative = path.relative_to(ROOT).as_posix()
            old = subprocess.run(
                ["git", "show", f"{base_ref}:{relative}"], cwd=ROOT, capture_output=True
            )
            if old.returncode != 0 or old.stdout.replace(
                b"\r\n", b"\n"
            ) != path.read_bytes().replace(b"\r\n", b"\n"):
                raise ValueError(
                    "new or changed legacy sample requires a human-reviewed reader-contract set"
                )
        legacy.append({
            "legacy_file": path.name,
            "fixture_count": 1,
            "covered_states": [],
            "last_real_capture_at": None,
            "reader_version": None,
            "raw_schema_version": None,
            "passed": False,
            "drift_reason": "legacy_without_reviewed_exact_host_contract",
            "production_support_proven": False,
        })
    contract_root = root / "reader-contract"
    if any(p != contract_root and (not p.is_file() or p.suffix != ".json") for p in root.iterdir()):
        raise ValueError("unlisted reader fixture directory or file")
    results = []
    if contract_root.exists():
        for directory in sorted(contract_root.iterdir()):
            if not directory.is_dir() or not (directory / "manifest.json").is_file():
                raise ValueError("unlisted reader fixture files")
            manifest = reviewed_manifest(directory)
            if not owner_authorized_candidate(manifest):
                verify_human_review(manifest["review"])
            if base_ref:
                relative = (directory / "manifest.json").relative_to(ROOT).as_posix()
                old = subprocess.run(
                    ["git", "show", f"{base_ref}:{relative}"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                if old.returncode == 0:
                    previous = json.loads(old.stdout)
                    if bundle_digest(previous) != bundle_digest(manifest) and stable_version(
                        manifest["fixture_set_version"]
                    ) <= stable_version(previous["fixture_set_version"]):
                        raise ValueError(
                            "changed fixture/envelope/golden requires reviewed version bump"
                        )
            selected = registry or DEFAULT_REGISTRY
            if registry is None and (
                manifest["evidence_kind"],
                manifest["reader_id"],
                manifest["brand"],
                manifest["hostname"],
            ) == ("synthetic", "synthetic_reference", "example", "reader.example.invalid"):
                # Authoring-only example. It never becomes a production registration.
                selected = reference_registry()
            report = run_fixture_set(directory, selected)
            results.append(report)
    if any(not result["passed"] for result in results):
        raise ValueError("reader golden contract drift")
    evidenced = {
        (r["reader_id"], r["reader_version"], r["brand"], r["hostname"], r["raw_schema_version"])
        for r in results
        if r["review_status"] == "approved" and r["evidence_kind"] == "sanitized_real"
    }
    if any(
        (r.reader_id, r.reader_version, r.brand, r.hostname, r.raw_schema_version) not in evidenced
        for r in DEFAULT_REGISTRY.registrations
    ):
        raise ValueError("production registration requires a reviewed passing fixture set")
    reviewed_count = sum(r["review_status"] == "approved" for r in results)
    candidate_count = len(results) - reviewed_count
    fixture_gate = "reviewed" if reviewed_count else "no_human_reviewed_fixture_sets"
    if candidate_count:
        fixture_gate = "owner_authorized_synthetic_pending_admin_review"
    return {
        "contract_version": 1,
        "safety_gate_passed": True,
        "published_fixture_sets": len(results),
        "reviewed_fixture_sets": reviewed_count,
        "publication_authorized_fixture_sets": candidate_count,
        "fixture_gate": fixture_gate,
        "integrations": results,
        "legacy_samples": legacy,
        "unmigrated_brand_readers": [
            {
                "brand": brand,
                "hostname": None,
                "fixture_count": sum(sample["legacy_file"] == brand + ".json" for sample in legacy),
                "covered_states": [],
                "last_real_capture_at": None,
                "reader_version": None,
                "raw_schema_version": None,
                "passed": False,
                "drift_reason": "unversioned_brand_reader_without_reviewed_exact_host_evidence",
                "production_support_proven": False,
            }
            for brand in sorted(LEITORES)
        ],
        "production_registration_count": len(DEFAULT_REGISTRY.registrations),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = validate(args.base_ref)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    else:
        sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
