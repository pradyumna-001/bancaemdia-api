"""Require the full real web API/PostgreSQL/Celery acceptance matrix, zero skips."""

import logging
import sys
import xml.etree.ElementTree as ET
from collections import Counter

REQUIRED = {
    "test_same_day_retry_reserves_exactly_one_attempt": 1,
    "test_newer_bet_write_becomes_retryable_failure_without_partial_materialization": 1,
    "test_known_stake_review_preserves_private_original": 1,
    "test_web_print_migration_roundtrip_refuses_populated_downgrade": 1,
    "test_multiple_files_real_worker_and_no_financial_replay": 1,
    "test_repeated_and_concurrent_identical_bytes_dedupe_per_user": 1,
    "test_lost_post_response_and_broker_outage_reconcile_without_replay": 1,
    "test_partial_failure_explicit_single_item_retry_and_same_retry_identity": 1,
    "test_identity_incompatible_bytes_is_deterministic_and_preserves_original": 1,
    "test_unknown_stake_goes_to_real_review_without_zero_bet_and_can_resolve": 2,
    "test_real_no_bet_result_is_distinct_from_failure": 1,
    "test_invalid_empty_active_and_truncated_images_are_not_accepted": 4,
    "test_real_raster_formats": 4,
    "test_mime_conflict_and_real_byte_limit": 1,
    "test_twenty_items_limit_does_not_discard_previously_accepted": 1,
    "test_daily_internal_limit_429_retry_after_and_replay_does_not_consume_slot": 1,
    "test_missing_session_is_401_and_foreign_retry_is_404": 1,
    "test_read_only_keeps_reconciliation_and_denies_new_input_and_retry": 1,
    "test_retention_expires_private_bytes_and_state_but_preserves_tombstones": 1,
    "test_real_worker_in_a_fresh_process_uses_isolated_extraction_adapter": 1,
    "test_cookie_csrf_and_real_revocation": 1,
    "test_forced_rls_role_and_cross_owner_media": 1,
    "test_malformed_multipart_and_missing_file_have_safe_codes": 1,
    "test_extra_files_are_rejected_instead_of_silently_discarded": 1,
    "test_original_reconciliation_reads_do_not_change_any_financial_table": 1,
    "test_cached_reading_and_expired_lease_recover_without_paid_extraction": 1,
    "test_duplicate_failed_alias_can_retry_once_without_new_financial_identity": 1,
    "test_internal_cost_and_failure_text_never_enter_public_review": 1,
    "test_real_raster_dimension_budget_is_enforced_on_intake": 1,
    "test_database_dependency_503_is_safe_and_has_retry_after": 1,
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
            f"Incomplete web-print acceptance: expected {REQUIRED}, received {dict(counts)}"
        )
    logging.warning(
        "Web-print acceptance: %s passed, zero missing/failed/skipped/duplicate cases", len(cases)
    )


if __name__ == "__main__":
    main()
