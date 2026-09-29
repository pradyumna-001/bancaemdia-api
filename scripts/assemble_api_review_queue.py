"""Rehearse the open API PR queue in a clean disposable checkout, without pushing.

Local fixture commits exist only to let Git simulate subsequent integrations.
They are never product merge candidates. Original review branches stay separate.
"""

import json
import logging
import sys
from pathlib import Path

from assemble_billing_cross_flow import git
from assemble_billing_cross_flow import main as assemble_billing

GENERATED = {"docs/API.md", "tests/contract/schemas/openapi.json"}
IDENTITY = ("-c", "user.name=API review fixture", "-c", "user.email=fixture@example.invalid")


def save_fixture(label: str, new_migration: str | None = None) -> None:
    git("add", "--update")
    if new_migration:
        git("add", new_migration)
    git(*IDENTITY, "commit", "-m", f"test-only: {label}")


def merge_with_patch(sha: str, patch: str, expected: set[str], new_migration: str | None) -> None:
    result = git(*IDENTITY, "merge", "--no-commit", "--no-ff", sha, check=False)
    conflicts = set(git("diff", "--name-only", "--diff-filter=U").stdout.splitlines())
    if result.returncode != 1 or conflicts - GENERATED != expected:
        raise SystemExit(
            f"Unexpected conflicts for {sha}: {result.returncode}, {sorted(conflicts)}"
        )
    git("restore", "--ours", "--worktree", "--", *sorted(conflicts))
    git("apply", patch)
    save_fixture(sha, new_migration)


def main() -> None:
    assemble_billing()  # Requires explicit --disposable-checkout and a clean checkout.
    save_fixture("billing + Telegram", "alembic/versions/f155cross2026_test_integration_merge.py")
    heads = json.loads(Path("tests/fixtures/api-review-heads.json").read_text(encoding="utf-8"))
    merge_with_patch(
        heads["158"],
        "tests/fixtures/review-analytics.patch",
        {
            "src/bancaemdia/api/openapi.py",
            "src/bancaemdia/api/v1/usuario.py",
            "src/bancaemdia/main.py",
            "tests/contract/test_openapi.py",
            "tests/unit/test_migrations.py",
            "tests/unit/test_models_canonical.py",
            "tests/unit/test_painel_migration.py",
            "tests/unit/test_rls_policies.py",
        },
        "alembic/versions/f158queue2026_test_integration_merge.py",
    )
    merge_with_patch(
        heads["156"],
        "tests/fixtures/review-calculators.patch",
        {
            "src/bancaemdia/api/openapi.py",
            "src/bancaemdia/main.py",
            "tests/contract/test_openapi.py",
        },
        None,
    )
    for number in ("130", "147", "148", "146", "144", "150", "157", "159", "131"):
        sha = heads[number]
        logging.info("Rehearsing PR #%s at %s", number, sha)
        result = git(
            *IDENTITY, "merge", "--no-ff", sha, "-m", f"test-only: PR {number}", check=False
        )
        if result.returncode:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(f"Unexpected conflict in PR #{number}; review the actual branch.")
    logging.info("All pinned PR sources assembled. Regenerate OpenAPI and run the full suite.")


if __name__ == "__main__":
    main()
