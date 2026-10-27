"""Frozen mandatory actual HTTP/PostgreSQL manual-creation inventory."""

import sys
import xml.etree.ElementTree as ET

EXPECTED = frozenset([
    "test_unexpected_creator_error_rolls_back_and_preserves_safe_recovery",
    "test_real_lost_201_after_commit_recovers_without_replay",
    "test_sequential_replay_normalized_body_freezes_original_confirmation",
    "test_multiple_concurrent_posts_converge_one_fact_event_receipt",
    "test_same_identity_other_body_conflicts_no_effect",
    "test_concurrent_different_bodies_one_wins_other_conflicts",
    "test_different_identities_equal_contents_are_distinct_legitimate_bets",
    "test_unknown_identity_404_has_no_state_or_rollback_claim",
    "test_uncommitted_intent_404_can_later_confirm_without_duplicate",
    "test_financial_face_cost_commission_dates_and_current_unit[False]",
    "test_financial_face_cost_commission_dates_and_current_unit[True]",
    "test_default_date_unit_and_assignment_not_recomputed_on_replay",
    "test_original_receipt_survives_later_bet_changes_never_resurrects[correct]",
    "test_original_receipt_survives_later_bet_changes_never_resurrects[delete]",
    "test_original_receipt_survives_later_bet_changes_never_resurrects[physical_delete]",
    "test_explicit_account_wrong_house_or_owner_safe_422[wrong_owner]",
    "test_explicit_account_wrong_house_or_owner_safe_422[wrong_house]",
    "test_game_time_historical_account_and_explicit_multicontas_exception",
    "test_missing_account_or_game_opens_one_typed_review_no_guess",
    "test_safe_field_validation_422_writes_nothing[fields0]",
    "test_safe_field_validation_422_writes_nothing[fields1]",
    "test_safe_field_validation_422_writes_nothing[fields2]",
    "test_safe_field_validation_422_writes_nothing[fields3]",
    "test_safe_field_validation_422_writes_nothing[fields4]",
    "test_safe_field_validation_422_writes_nothing[fields5]",
    "test_safe_field_validation_422_writes_nothing[fields6]",
    "test_safe_field_validation_422_writes_nothing[fields7]",
    "test_safe_field_validation_422_writes_nothing[fields8]",
    "test_safe_field_validation_422_writes_nothing[fields9]",
    "test_safe_field_validation_422_writes_nothing[fields10]",
    "test_safe_field_validation_422_writes_nothing[fields11]",
    "test_invalid_uuid_and_malformed_json_are_safe_422",
    "test_401_requires_reauthentication_and_has_no_effect",
    "test_owner_isolation_reference_not_authorization_and_forced_rls",
    "test_atomic_receipt_failure_rolls_back_bet_events_and_account_review",
    "test_legacy_post_remains_compatible_with_no_implicit_idempotency",
    "test_expiry_keeps_identity_occupied_and_immutable_no_recreation",
    "test_402_read_only_recovers_saved_receipt_without_payment_or_replay",
    "test_explicit_bigint_and_receipt_money_are_exact_strings",
    "test_legacy_ambiguous_usage_is_real_http_review_without_arbitrary_account",
    "test_no_game_time_does_not_use_placement_day_for_account",
    "test_cookie_csrf_origin_and_revocation_actual_identity",
    "test_busy_timeout_has_safe_code_retry_after_no_partial_write",
    "test_rate_limit_publishes_429_retry_after_without_creating",
    "test_actual_api_process_restart_recovers_database_receipt",
])


def main() -> None:
    root = ET.parse(sys.argv[1]).getroot()
    actual = [c.attrib["name"] for c in root.iter("testcase")]
    assert len(actual) == len(set(actual)) == len(EXPECTED), "missing/duplicated manual acceptance"
    assert set(actual) == EXPECTED, "mandatory manual acceptance inventory changed or omitted"
    assert not any(list(root.iter(tag)) for tag in ("failure", "error", "skipped")), (
        "manual acceptance cannot fail or skip"
    )
    sys.stdout.write(
        f"Manual creation acceptance: {len(actual)} passed, zero skips/failures/omissions/duplicates"
    )


if __name__ == "__main__":
    main()
