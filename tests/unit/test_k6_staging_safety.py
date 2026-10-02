"""The staging seeder must refuse a workstation or a path containing repository data."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/k6_staging.py"
SPEC = importlib.util.spec_from_file_location("k6_staging", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
staging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging)


def test_refuses_non_ci_machine(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("K6_STAGING_DIR", str(tmp_path / "runtime"))
    with pytest.raises(RuntimeError, match="isolated GitHub Actions"):
        staging.runtime_dir()


@pytest.mark.parametrize("destination", ["same", "outside", "traversal"])
def test_refuses_unowned_destination(monkeypatch, tmp_path, destination):
    monkeypatch.setattr(staging.sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    runner = tmp_path / "runner"
    paths = {
        "same": runner,
        "outside": tmp_path / "elsewhere",
        "traversal": runner / ".." / "escape",
    }
    monkeypatch.setenv("RUNNER_TEMP", str(runner))
    monkeypatch.setenv("K6_STAGING_DIR", str(paths[destination]))
    with pytest.raises(RuntimeError, match="child of RUNNER_TEMP"):
        staging.runtime_dir()


def test_refuses_credentials_inside_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(staging.sys, "platform", "linux")
    monkeypatch.setattr(staging, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("K6_STAGING_DIR", str(tmp_path / "runtime"))
    with pytest.raises(RuntimeError, match="outside the checkout"):
        staging.runtime_dir()


def test_accepts_only_isolated_runner_child(monkeypatch, tmp_path):
    monkeypatch.setattr(staging.sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("K6_STAGING_DIR", str(tmp_path / "runtime"))
    assert staging.runtime_dir() == tmp_path / "runtime"
