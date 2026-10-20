"""Require every independent hypothetical PostgreSQL acceptance case, with no skips."""

import logging
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

REQUIRED = {
    "test_commercial_read_queries_once_without_caching_live_permission": 1,
    "test_bigint_aggregate_roi_with_zero_stakes_has_exact_six_decimals": 1,
    "test_confirmed_tip_origins_use_canonical_link_sources": 3,
    "test_historical_example_keeps_real_odd_result_and_does_not_count_tip": 1,
    "test_all_states_have_independent_financial_expectations": 7,
    "test_rounding_half_even_at_each_step": 3,
    "test_unlinked_rejected_ambiguous_equal_stakes_remain_real": 5,
    "test_unsafe_data_is_explicit_and_never_a_complete_comparison": 11,
    "test_freebet_uses_face_and_zero_cost_not_proportional_bonus": 4,
    "test_full_population_larger_than_page_and_no_n_plus_one": 1,
    "test_all_selectors_use_owned_canonical_population": 8,
    "test_dates_timezone_missing_dates_deleted_review_and_sources": 1,
    "test_foreign_and_nonexistent_private_selectors_are_indistinguishable": 4,
    "test_rls_foreign_tip_fk_and_unauthenticated_private_response": 1,
    "test_read_only_permission_and_real_facts_events_caixa_exports_unchanged": 1,
    "test_invalid_inputs_are_rejected_privately": 9,
    "test_real_postgres_timeout_is_503_not_empty_success": 1,
    "test_bigint_money_empty_and_published_examples_match_full_openapi": 1,
    "test_over_limit_population_is_rejected_without_truncation": 1,
    "test_missing_or_multiple_evidence_never_selects_arbitrary_tip": 2,
    "test_partial_comparison_does_not_publish_sum_of_safe_subset": 1,
    "test_standard_settlement_is_shared_with_existing_financial_core": 14,
    "test_exact_half_settlement_above_binary_integer_precision": 1,
}


def check(path: Path) -> int:
    cases = ElementTree.parse(path).getroot().findall(".//testcase")
    counts = Counter(case.attrib["name"].split("[", 1)[0] for case in cases)
    if any(counts[name] != count for name, count in REQUIRED.items()):
        raise ValueError("Mandatory hypothetical acceptance inventory is incomplete")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("Hypothetical acceptance requires every case passing with zero skips")
    return len(cases)


if __name__ == "__main__":
    logging.warning("%s hypothetical cases passed with zero skips", check(Path(sys.argv[1])))
