"""CI must fail if a parameterized race silently disappears or skips."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/check_integrity_junit.py"
SPEC = importlib.util.spec_from_file_location("integrity_gate", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


@pytest.mark.parametrize("bad", ["missing", "skip", "error", "failure", "duplicate", "unknown"])
def test_integrity_gate_rejects_incomplete_acceptance(tmp_path, bad):
    manifest = tmp_path / "required.json"
    manifest.write_text(
        json.dumps([
            {"module": "races", "name": "test_delivery[1]"},
            {"module": "races", "name": "test_delivery[10]"},
        ])
    )
    first = '<testcase classname="tests.races" name="test_delivery[1]" />'
    second = '<testcase classname="tests.races" name="test_delivery[10]" />'
    if bad == "missing":
        second = ""
    elif bad in {"skip", "error", "failure"}:
        tag = "skipped" if bad == "skip" else bad
        second = f'<testcase classname="tests.races" name="test_delivery[10]"><{tag}/></testcase>'
    elif bad == "duplicate":
        second += second
    else:
        second = '<testcase classname="tests.races" name="test_other" />'
    junit = tmp_path / "result.xml"
    junit.write_text(f"<testsuites><testsuite>{first}{second}</testsuite></testsuites>")
    with pytest.raises(ValueError):
        GATE.check(junit, manifest)


def test_integrity_gate_accepts_exact_names_across_suites(tmp_path):
    manifest = tmp_path / "required.json"
    manifest.write_text(json.dumps([{"module": "races", "name": "test_delivery[10]"}]))
    junit = tmp_path / "result.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase classname="tests.races" name="test_delivery[10]" /></testsuite></testsuites>'
    )
    assert GATE.check(junit, manifest) == 1
