"""Frozen mandatory review API/PostgreSQL inventory; no omissions or skips."""

import sys
import xml.etree.ElementTree as ET

EXPECTED = frozenset([
    "test_evidence_token_uses_one_query_and_reobserves_catalog_changes",
    "test_fresh_read_of_row_only_writer_cannot_overwrite_financial_evidence[odd]",
    "test_fresh_read_of_row_only_writer_cannot_overwrite_financial_evidence[stake_centavos]",
    "test_unknown_legacy_reading_stake_remains_null_until_human_completion",
    "test_account_choice_does_not_approve_independent_extraction_review",
    "test_explicit_side_never_borrows_other_tipster_identity[casa]",
    "test_explicit_side_never_borrows_other_tipster_identity[telegram]",
    "test_401_reauth_without_mutation",
    "test_402_preserves_reads_receipt_and_form_without_payment_replay",
    "test_account_absent_ambiguous_without_photo_no_implicit_choice[0]",
    "test_account_absent_ambiguous_without_photo_no_implicit_choice[2]",
    "test_account_explicit_string_exact_bigint_not_coerced_by_client",
    "test_atomic_rollback_when_receipt_flush_fails_in_real_database",
    "test_concurrent_decisions_close_once_and_duplicate_intention_recovers",
    "test_correction_with_private_photo_safe_fields_and_exact_money",
    "test_domain_busy_has_retry_after_and_no_changes",
    "test_explicit_account_string_revalidated_safe_error[wrong_house]",
    "test_explicit_account_string_revalidated_safe_error[wrong_owner]",
    "test_historical_closed_no_holder_account_uses_game_day_and_explicit_exception",
    "test_invalid_correction_safe_code_field_no_internal_dumps[fields0]",
    "test_invalid_correction_safe_code_field_no_internal_dumps[fields1]",
    "test_invalid_correction_safe_code_field_no_internal_dumps[fields2]",
    "test_invalid_correction_safe_code_field_no_internal_dumps[fields3]",
    "test_invalid_or_ignored_age_filters_are_refused[query0]",
    "test_invalid_or_ignored_age_filters_are_refused[query1]",
    "test_invalid_or_ignored_age_filters_are_refused[query2]",
    "test_legacy_pending_pair_adopts_shared_fact_or_restores_distinct_tip[DISTINTAS]",
    "test_legacy_pending_pair_adopts_shared_fact_or_restores_distinct_tip[MESMA]",
    "test_legacy_resolver_accepts_explicit_tipster_without_ids",
    "test_legacy_writer_resolution_not_attributed_to_unknown_intention",
    "test_modern_reject_link_keeps_both_original_financial_facts[DISTINTAS]",
    "test_modern_reject_link_keeps_both_original_financial_facts[RECUSAR_VINCULO]",
    "test_null_return_and_freebet_face_preserved",
    "test_opened_interval_brazil_bounds_filtered_counts_and_consumption_restart",
    "test_other_owner_read_write_recovery_media_and_forced_rls",
    "test_pair_and_context_changes_invalidate_seen_decision[candidate]",
    "test_pair_and_context_changes_invalidate_seen_decision[competitor]",
    "test_pair_and_context_changes_invalidate_seen_decision[dictionary]",
    "test_pair_and_context_changes_invalidate_seen_decision[link]",
    "test_private_review_media_mime_size_and_corruption_limits[corrupt]",
    "test_private_review_media_mime_size_and_corruption_limits[mime]",
    "test_private_review_media_mime_size_and_corruption_limits[size]",
    "test_real_response_lost_after_commit_get_receipt_without_replay",
    "test_receipt_expiry_immutable_audit_and_forced_rls",
    "test_review_cookie_csrf_and_real_revocation",
    "test_server_conflicting_tipsters_publish_choices_and_one_financial_fact[casa]",
    "test_server_conflicting_tipsters_publish_choices_and_one_financial_fact[telegram]",
    "test_stale_evidence_legacy_intermediate_writers_no_partial_effects[account]",
    "test_stale_evidence_legacy_intermediate_writers_no_partial_effects[event]",
    "test_stale_evidence_legacy_intermediate_writers_no_partial_effects[legacy_endpoint]",
    "test_stale_evidence_legacy_intermediate_writers_no_partial_effects[raw_bet]",
    "test_stale_evidence_legacy_intermediate_writers_no_partial_effects[review]",
    "test_stale_evidence_legacy_intermediate_writers_no_partial_effects[usage]",
    "test_unknown_reason_explicit_support_without_arbitrary_actions",
    "test_unmaterialized_print_routes_to_existing_real_materializer[CORRIGIR]",
    "test_unmaterialized_print_routes_to_existing_real_materializer[DESCARTAR]",
    "test_unsupported_family_even_when_bet_is_under_review",
])


def main() -> None:
    root = ET.parse(sys.argv[1]).getroot()
    actual = [case.attrib["name"] for case in root.iter("testcase")]
    assert len(actual) == len(set(actual)) == len(EXPECTED), (
        "missing or duplicated mandatory reviews proof"
    )
    assert set(actual) == EXPECTED, "mandatory reviews inventory changed"
    for tag in ("skipped", "failure", "error"):
        assert not list(root.iter(tag)), f"mandatory reviews acceptance contains {tag}"
    sys.stdout.write(
        f"Review acceptance: {len(actual)} passed, zero skips/failures/missing/duplicates\n"
    )


if __name__ == "__main__":
    main()
