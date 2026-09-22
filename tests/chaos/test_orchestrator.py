"""The staging fault injector must be inert until its target is confirmed."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.chaos
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "chaos_inject.py"
TARGET = [
    "staging-failover",
    "--rds-instance",
    "staging-db",
    "--aws-profile",
    "staging",
    "--region",
    "us-east-1",
    "--ready-url",
    "https://staging.example/ready",
]


def test_staging_fault_is_dry_run_by_default() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *TARGET], capture_output=True, text=True, check=True
    )
    assert "Dry run:" in result.stdout
    assert "--force-failover" in result.stdout


def test_staging_execution_rejects_an_unconfirmed_target() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *TARGET, "--execute", "--confirm-instance", "other-db"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--confirm-instance must exactly match" in result.stderr
