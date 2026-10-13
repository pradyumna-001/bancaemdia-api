"""Frozen mandatory result API/PostgreSQL inventory; no omissions or skips."""

import sys
import xml.etree.ElementTree as ET

EXPECTED = frozenset([
    "test_account_erasure_removes_receipts_and_preimages",
    "test_busy_lock_and_unavailable_are_distinct_from_stale_revision",
    "test_consolidation_invalidates_revision_and_excludes_contextual_source",
    "test_cookie_csrf_and_real_revocation",
    "test_cursor_binding_and_new_insert_fence",
    "test_expiry_preserves_financial_facts",
    "test_filters_isolated_combined_and_authorized_labels",
    "test_financial_states_and_exact_undo[ANULADA-False-None-10000]",
    "test_financial_states_and_exact_undo[ANULADA-True-None-0]",
    "test_financial_states_and_exact_undo[CASHOUT-False-7531-7531]",
    "test_financial_states_and_exact_undo[CASHOUT-True-7531-7531]",
    "test_financial_states_and_exact_undo[GREEN-False-None-20000]",
    "test_financial_states_and_exact_undo[GREEN-True-None-10000]",
    "test_financial_states_and_exact_undo[MEIO_GREEN-False-None-15000]",
    "test_financial_states_and_exact_undo[MEIO_GREEN-True-None-5000]",
    "test_financial_states_and_exact_undo[MEIO_RED-False-None-5000]",
    "test_financial_states_and_exact_undo[MEIO_RED-True-None-0]",
    "test_financial_states_and_exact_undo[PENDENTE-False-None-None]",
    "test_financial_states_and_exact_undo[RED-False-None-0]",
    "test_financial_states_and_exact_undo[RED-True-None-0]",
    "test_game_day_account_and_explicit_multicont_exception",
    "test_global_keyset_order_ties_nulls_and_shrinking_population",
    "test_ineligible_items_are_decided_by_server[deleted-deleted]",
    "test_ineligible_items_are_decided_by_server[severe-severe_review]",
    "test_ineligible_items_are_decided_by_server[unassigned-account_unassigned]",
    "test_intermediate_writer_invalidates_intention[before_result-correction]",
    "test_intermediate_writer_invalidates_intention[before_result-deleted_restored]",
    "test_intermediate_writer_invalidates_intention[before_result-event_only]",
    "test_intermediate_writer_invalidates_intention[before_result-external_same_state]",
    "test_intermediate_writer_invalidates_intention[before_result-legacy_result]",
    "test_intermediate_writer_invalidates_intention[before_result-review_resolution]",
    "test_intermediate_writer_invalidates_intention[before_undo-correction]",
    "test_intermediate_writer_invalidates_intention[before_undo-deleted_restored]",
    "test_intermediate_writer_invalidates_intention[before_undo-event_only]",
    "test_intermediate_writer_invalidates_intention[before_undo-external_same_state]",
    "test_intermediate_writer_invalidates_intention[before_undo-legacy_result]",
    "test_intermediate_writer_invalidates_intention[before_undo-review_resolution]",
    "test_invalid_result_no_partial_facts[fields0]",
    "test_invalid_result_no_partial_facts[fields1]",
    "test_invalid_result_no_partial_facts[fields2]",
    "test_invalid_result_no_partial_facts[fields3]",
    "test_invalid_result_no_partial_facts[fields4]",
    "test_invalid_result_no_partial_facts[fields5]",
    "test_invalid_result_no_partial_facts[fields6]",
    "test_invalid_result_no_partial_facts[fields7]",
    "test_invalid_result_no_partial_facts[fields8]",
    "test_invalid_result_no_partial_facts[fields9]",
    "test_isolation_queue_write_recovery_and_undo",
    "test_lost_response_recovery_payload_conflict_and_repeated_undo",
    "test_migration_empty_roundtrip_and_populated_refusal",
    "test_queue_exceeds_ten_thousand_without_truncation",
    "test_read_only_preserves_session_reads_recovery_and_export",
    "test_restore_previous_settled_state_exactly[calculated]",
    "test_restore_previous_settled_state_exactly[cashout]",
    "test_restore_previous_settled_state_exactly[explicit]",
    "test_restore_previous_settled_state_exactly[null_commission]",
    "test_restore_previous_settled_state_exactly[unknown]",
    "test_same_identity_concurrent_delivery_commits_one_fact",
    "test_twenty_gestures_twenty_settlements_duplicate_delivery",
    "test_two_intentions_same_revision_exactly_one_wins",
    "test_unauthenticated_private_get_and_write",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params0]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params1]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params2]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params3]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params4]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params5]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params6]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params7]",
    "test_unsupported_or_invalid_queue_parameters_are_refused[params8]",
])


def main() -> None:
    root = ET.parse(sys.argv[1]).getroot()
    actual = [case.attrib["name"] for case in root.iter("testcase")]
    assert len(actual) == len(set(actual)) == len(EXPECTED), (
        "missing or duplicated mandatory results proof"
    )
    assert set(actual) == EXPECTED, "mandatory results inventory changed"
    for tag in ("skipped", "failure", "error"):
        assert not list(root.iter(tag)), f"mandatory results acceptance contains {tag}"
    sys.stdout.write(
        f"Results acceptance: {len(actual)} passed, zero skips/failures/missing/duplicates\n"
    )


if __name__ == "__main__":
    main()
