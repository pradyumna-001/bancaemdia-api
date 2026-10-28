"""Require the entire real PostgreSQL/API/S3 photo matrix, without skipped proofs."""

import logging
import sys
import xml.etree.ElementTree as ET
from collections import Counter

REQUIRED = {
    "test_ownership_deduplication_known_hash_and_complete_read_invariance": 1,
    "test_review_resolution_and_deleted_restored_bet_keep_original": 1,
    "test_exact_resolved_review_provenance_and_ambiguity_fail_closed": 1,
    "test_real_validated_formats_are_delivered_without_modification": 4,
    "test_invalid_missing_and_oversized_database_images": 9,
    "test_s3_authenticated_api_transport_and_storage_errors": 5,
    "test_read_only_is_explicitly_allowed_and_photo_has_no_financial_writes": 1,
    "test_401_and_private_headers_for_session_failure": 4,
    "test_photo_quota_uses_existing_429_retry_after": 1,
    "test_original_export_ingestion_and_replay_preserve_provenance": 1,
    "test_bot_confirmation_private_image_and_retention": 7,
    "test_real_cookie_session_revocation_blocks_previous_photo_access": 1,
    "test_private_validation_and_dependency_failures": 6,
    "test_photo_reads_preserve_nonempty_consolidations_and_cash": 1,
}


def main() -> None:
    cases = list(ET.parse(sys.argv[1]).getroot().iter("testcase"))
    counts = Counter(case.attrib["name"].split("[", 1)[0] for case in cases)
    identities = {(case.attrib.get("classname"), case.attrib["name"]) for case in cases}
    if (
        counts != REQUIRED
        or len(identities) != len(cases)
        or any(
            case.find(kind) is not None
            for case in cases
            for kind in ("failure", "error", "skipped")
        )
    ):
        raise SystemExit(
            f"Incomplete photo acceptance: expected {REQUIRED}, received {dict(counts)}"
        )
    logging.warning(
        "Photo acceptance: %s passed, zero missing/failed/skipped/duplicate cases", len(cases)
    )


if __name__ == "__main__":
    main()
