"""Assemble this PR HEAD with the exact Telegram prerequisite in a disposable checkout.

Never commits, pushes, or rewrites a review branch. The fixture patch resolves
only the recorded integration conflicts; an unexpected conflict fails closed.
"""

import argparse
import logging
import subprocess
from pathlib import Path

TELEGRAM = "71ab6b591325387122a2315133ee7d32b4e4f0d6"
CONFLICTS = {
    "docs/API.md",
    "src/bancaemdia/api/openapi.py",
    "src/bancaemdia/main.py",
    "src/bancaemdia/workers/celery_app.py",
    "tests/contract/schemas/openapi.json",
    "tests/contract/test_openapi.py",
    "tests/unit/test_migrations.py",
    "tests/unit/test_models_canonical.py",
    "tests/unit/test_painel_migration.py",
    "tests/unit/test_rls_policies.py",
}


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=check, text=True, capture_output=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-checkout", action="store_true", required=True)
    parser.parse_args()
    if git("status", "--porcelain").stdout.strip():
        raise SystemExit("Use a clean, disposable checkout; this command writes a test fixture.")
    logging.info("Billing test HEAD: %s", git("rev-parse", "HEAD").stdout.strip())
    logging.info("Telegram prerequisite: %s", TELEGRAM)
    if git("merge-base", "--is-ancestor", TELEGRAM, "HEAD", check=False).returncode == 0:
        logging.info("Telegram prerequisite already belongs to this HEAD.")
        return
    result = git(
        "-c",
        "user.name=Billing integration fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "merge",
        "--no-commit",
        "--no-ff",
        TELEGRAM,
        check=False,
    )
    actual = set(git("diff", "--name-only", "--diff-filter=U").stdout.splitlines())
    if result.returncode != 1 or actual != CONFLICTS:
        raise SystemExit(f"Unexpected integration shape: {result.returncode}, {sorted(actual)}")
    git("restore", "--ours", "--worktree", "--", *sorted(CONFLICTS))
    git("apply", str(Path("tests/fixtures/billing-cross-flow.patch")))
    logging.info("Applied reviewed conflict resolutions; regenerate OpenAPI before testing.")


if __name__ == "__main__":
    main()
