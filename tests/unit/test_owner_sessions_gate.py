"""The mandatory owner-session inventory cannot silently lose an acceptance."""

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "owner_gate", ROOT / "scripts/check_owner_sessions_junit.py"
)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


@pytest.mark.parametrize(
    "mutation", ["none", "missing", "duplicate", "skip", "failure", "error", "unknown"]
)
def test_owner_gate_enforces_every_frozen_case(tmp_path, mutation):
    names = json.loads((ROOT / "scripts/owner-sessions-required.json").read_text())
    suite = ET.Element("testsuite")
    for name in names:
        ET.SubElement(
            suite, "testcase", classname="tests.integration.coleta.test_owner_sessions", name=name
        )
    if mutation == "missing":
        suite.remove(suite[0])
    elif mutation == "duplicate":
        ET.SubElement(suite, "testcase", **suite[0].attrib)
    elif mutation in {"skip", "failure", "error"}:
        ET.SubElement(suite[0], "skipped" if mutation == "skip" else mutation)
    elif mutation == "unknown":
        suite[0].set("name", "test_unreviewed_replacement")
    path = tmp_path / "report.xml"
    ET.ElementTree(suite).write(path)
    if mutation == "none":
        assert GATE.check(path) == 26
    else:
        with pytest.raises(ValueError):
            GATE.check(path)
