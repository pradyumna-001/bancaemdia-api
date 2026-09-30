"""No acceptance can be replaced by skipped or absent integration cases."""

import sys
from pathlib import Path
from xml.etree import ElementTree

mode = sys.argv[1]
path = (
    Path("catalog-main.xml")
    if mode == "main"
    else Path("/tmp/catalog-integration/catalog-installation.xml")
)
cases = list(ElementTree.parse(path).iter("testcase"))
required = {
    "test_dry_run_persists_nothing_and_apply_is_idempotent",
    "test_source_change_removal_redirect_preserves_history_and_support",
    "test_manual_confirmation_is_owned_idempotent_and_no_secrets",
    "test_rls_cannot_self_grant_operator_or_read_foreign_confirmations",
    "test_transaction_rollback_restores_catalog_sources_and_audit",
    "test_real_concurrent_sync_converges_without_duplicate_snapshot",
    "test_publication_monotonic_signed_and_readable_without_regulatory_data",
    "test_no_hash_mismatch_or_stale_source_can_mutate_catalog",
    "test_operator_authorization_is_not_a_claim_or_user_choice",
}
if mode == "installation":
    required |= {
        "test_real_pairing_gets_signed_exact_catalog_and_conditional_cache",
        "test_stale_publication_refuses_304_and_cannot_extend_offline_bounds",
        "test_revocation_and_rotation_are_checked_even_with_valid_etag",
        "test_retrieval_uses_installation_credentials_not_bearer_or_legacy_token",
        "test_revoked_exact_host_remains_signed_tombstone_and_preserves_old_catalog",
    }
assert required <= {c.attrib["name"].split("[", 1)[0] for c in cases}, "required acceptance missing"
assert all(not list(c) for c in cases), "acceptance has skipped, failed or errored cases"
assert len(cases) == len({(c.attrib.get("classname"), c.attrib["name"]) for c in cases}), (
    "duplicate acceptance case"
)
