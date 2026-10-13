"""Enforce the complete mandatory spreadsheet acceptance inventory, without skips."""

import logging
import sys
import xml.etree.ElementTree as ET

EXPECTED = frozenset({
    "test_migration_refuses_populated_downgrade_and_restores_empty_guards",
    "test_parser_time_budget_refuses_without_changing_financial_snapshot",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[overflow]",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[boolean]",
    "test_preview_remap_and_read_pages_have_zero_financial_effects",
    "test_swapped_columns_are_corrected_without_losing_invalid_values",
    "test_valid_confirmation_real_materializer_lost_response_and_same_intent",
    "test_concurrent_same_confirmation_never_replays_financial_facts",
    "test_changed_file_mapping_hash_or_revision_refuses_old_confirmation[file]",
    "test_changed_file_mapping_hash_or_revision_refuses_old_confirmation[mapping]",
    "test_changed_file_mapping_hash_or_revision_refuses_old_confirmation[hash]",
    "test_changed_file_mapping_hash_or_revision_refuses_old_confirmation[revision]",
    "test_expired_preview_and_result_tombstones_cannot_reapply",
    "test_financial_concurrent_change_marks_obsolete_and_requires_new_validation",
    "test_stable_source_row_ids_survive_layout_changes_without_overwriting[same]",
    "test_stable_source_row_ids_survive_layout_changes_without_overwriting[reorder]",
    "test_stable_source_row_ids_survive_layout_changes_without_overwriting[rename]",
    "test_stable_source_row_ids_survive_layout_changes_without_overwriting[insert]",
    "test_stable_source_row_ids_survive_layout_changes_without_overwriting[remove]",
    "test_newer_versions_update_and_equal_or_older_versions_ignore",
    "test_freebet_and_unknown_optional_values_are_not_invented",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[duplicate_header]",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[duplicate_id]",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[date]",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[decimal]",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[formula]",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[text_formula]",
    "test_invalid_rows_and_headers_preserve_raw_values_and_refuse_atomic_apply[null]",
    "test_empty_corrupt_or_truncated_xlsx_is_refused[]",
    "test_empty_corrupt_or_truncated_xlsx_is_refused[not-xlsx]",
    "test_empty_corrupt_or_truncated_xlsx_is_refused[PK\\x03\\x04broken]",
    "test_bounded_structures_and_malicious_archives[bytes]",
    "test_bounded_structures_and_malicious_archives[rows]",
    "test_bounded_structures_and_malicious_archives[columns]",
    "test_bounded_structures_and_malicious_archives[sheets]",
    "test_bounded_structures_and_malicious_archives[expansion]",
    "test_bounded_structures_and_malicious_archives[external]",
    "test_preview_identity_conflict_and_source_reconciliation",
    "test_owner_isolation_for_source_preview_rows_remap_and_confirmation",
    "test_429_retry_after_and_safe_503_do_not_invent_result",
    "test_unauthenticated_is_401_and_catalog_publishes_backend_types",
    "test_actual_lost_confirmation_response_is_reconciled_without_replay",
    "test_read_only_reads_previous_imports_but_denies_new_preview_and_confirmation",
    "test_confirmation_identity_with_incompatible_preview_is_refused",
    "test_duplicate_mapping_and_unsupported_sheet_are_explicit",
    "test_legacy_direct_import_is_preserved_and_cannot_mix_registered_source",
    "test_missing_file_extra_fields_and_active_xml_have_safe_errors",
    "test_brazilian_decimals_and_synthetic_fixture_are_server_normalized",
    "test_expiration_purge_keeps_finances_and_durable_identity",
    "test_game_day_account_and_explicit_multicontas",
    "test_cookie_csrf_and_real_revocation",
})


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_sheet_import_results.py sheet-import-results.xml")
    cases = list(ET.parse(sys.argv[1]).getroot().iter("testcase"))
    names = [case.attrib["name"] for case in cases]
    if (
        set(names) != EXPECTED
        or len(names) != len(EXPECTED)
        or any(
            case.find(kind) is not None
            for case in cases
            for kind in ("failure", "error", "skipped")
        )
    ):
        raise SystemExit(
            "Incomplete spreadsheet acceptance: missing/duplicate/failed/skipped cases"
        )
    logging.warning(
        "Spreadsheet acceptance: %s passed; zero missing/duplicate/failed/skipped", len(cases)
    )


if __name__ == "__main__":
    main()
