"""Freeze every web-owner PostgreSQL acceptance even inside the pairing report."""

import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def check(path: Path) -> int:
    names = json.loads(
        (Path(__file__).with_name("owner-sessions-required.json")).read_text(encoding="utf-8")
    )
    expected = Counter(names)
    if len(names) != 26 or len(expected) != 26:
        raise ValueError("Invalid owner-session acceptance inventory")
    cases = [
        case
        for case in ET.parse(path).getroot().iter("testcase")
        if case.get("classname", "").rsplit(".", 1)[-1] == "test_owner_sessions"
    ]
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("Owner-session acceptance has failure/error/skip")
    if Counter(case.get("name", "") for case in cases) != expected:
        raise ValueError("Owner-session acceptance omitted, duplicated or added a case")
    return len(cases)


if __name__ == "__main__":
    sys.stdout.write(
        f"Verified {check(Path(sys.argv[1]))} owner-session PostgreSQL/HTTP cases, zero skips/failures/omissions\n"
    )
