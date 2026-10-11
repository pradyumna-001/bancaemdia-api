"""Reject missing, duplicated or skipped real daily/hierarchy acceptance."""

import logging
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

REQUIRED = {
    "test_hierarchy_historical_identity_and_full_group_totals": 1,
    "test_pairs_pagination_visits_every_child_without_changing_totals": 1,
    "test_daily_states_exact_profit_and_year_timezone_boundary": 1,
    "test_every_period_is_daily_and_fully_navigable": 5,
    "test_filters_and_complete_excel_reconcile": 5,
    "test_missing_dates_no_tipster_empty_history_and_previous_window": 1,
    "test_cursor_privacy_tampering_cross_owner_filter_and_projection": 1,
    "test_foreign_selectors_are_indistinguishable_from_missing": 12,
    "test_read_only_new_reads_and_exports_remain_available": 1,
    "test_real_statement_timeout_returns_private_503": 3,
    "test_rate_limits_and_expired_cursor_are_private": 1,
    "test_bigint_identity_and_money_remain_exact_in_json_and_excel": 1,
    "test_long_history_query_count_is_bounded_and_groups_not_n_plus_one": 1,
    "test_canonical_sources_do_not_duplicate_group_or_daily_finances": 1,
    "test_actual_materialization_delete_restore_keeps_same_group_fact": 1,
}


def check(path: Path) -> int:
    cases = ElementTree.parse(path).getroot().findall(".//testcase")
    counts = Counter(case.attrib["name"].split("[", 1)[0] for case in cases)
    if counts != Counter(REQUIRED):
        raise ValueError("Required panel acceptance inventory is incomplete or duplicated")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError(
            "Daily and hierarchy acceptance requires all cases passing with zero skips"
        )
    return len(cases)


if __name__ == "__main__":
    logging.warning("%s daily/hierarchy cases passed with zero skips", check(Path(sys.argv[1])))
