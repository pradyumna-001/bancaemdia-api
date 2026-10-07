"""Require every independent site-filter PostgreSQL acceptance case, with no skips."""

import logging
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

REQUIRED = {
    "test_visibility_pagination_and_empty_page_match_independent_sets": 3,
    "test_each_dimension_selects_before_page_and_without_duplicate_group_facts": 8,
    "test_state_origin_review_and_timezone_boundaries": 6,
    "test_private_foreign_ids_and_nonexistent_ids_are_indistinguishable": 4,
    "test_rls_without_user_and_cross_owner_group_fk": 1,
    "test_financial_summary_daily_graphs_and_all_xlsx_sections_use_independent_amounts": 1,
    "test_unknown_and_missing_dates_are_explicit_not_fabricated_zero": 1,
    "test_catalog_page_search_selected_inactive_and_exact_bigint": 1,
    "test_invalid_or_conflicting_filters_are_not_silently_removed": 7,
    "test_legacy_queries_default_and_all_remain_compatible": 1,
    "test_delete_restore_preserve_identity_events_and_groups": 1,
    "test_financial_cases_have_independent_expected_values": 7,
    "test_group_commands_archive_historical_links_and_audit_atomically": 1,
    "test_gets_are_read_only_and_origins_are_actual_domain_ids": 1,
    "test_explicit_bank_correction_is_owned_audited_and_replayable": 1,
    "test_catalog_database_timeout_is_503_not_a_successful_empty_list": 1,
    "test_consolidated_sources_preserve_visibility_without_double_financial_count": 6,
}


def check(path: Path) -> int:
    cases = ElementTree.parse(path).getroot().findall(".//testcase")
    counts = Counter(case.attrib["name"].split("[", 1)[0] for case in cases)
    if any(counts[name] != count for name, count in REQUIRED.items()):
        raise ValueError("Mandatory site-filter acceptance inventory is incomplete")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("Site-filter acceptance requires every case passing with zero skips")
    return len(cases)


if __name__ == "__main__":
    logging.warning("%s site-filter cases passed with zero skips", check(Path(sys.argv[1])))
