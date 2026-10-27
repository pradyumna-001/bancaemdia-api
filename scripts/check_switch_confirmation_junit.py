"""Freeze every temporal switch HTTP/PostgreSQL acceptance."""

import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def check(path: Path) -> int:
    names = json.loads(
        (Path(__file__).with_name("switch-confirmation-required.json")).read_text(encoding="utf-8")
    )
    expected = Counter(names)
    if len(names) != 34 or len(expected) != 34:
        raise ValueError("Invalid switch-confirmation acceptance inventory")
    cases = [
        case
        for case in ET.parse(path).getroot().iter("testcase")
        if case.get("classname", "").rsplit(".", 1)[-1] == "test_troca_confirmacao_db"
    ]
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("Switch-confirmation acceptance has failure/error/skip")
    if Counter(case.get("name", "") for case in cases) != expected:
        raise ValueError("Switch-confirmation acceptance omitted, duplicated or added a case")
    return len(cases)


if __name__ == "__main__":
    sys.stdout.write(
        f"Verified {check(Path(sys.argv[1]))} switch-confirmation PostgreSQL/HTTP cases, zero skips/failures/omissions\n"
    )
