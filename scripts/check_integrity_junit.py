"""Require the exact reviewed case inventory, including every parameterized scenario."""

import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def check(path: Path, manifest: Path | None = None) -> int:
    manifest = manifest or Path(__file__).parent / "integrity/required-cases.json"
    expected = Counter(
        (case["module"], case["name"]) for case in json.loads(manifest.read_text(encoding="utf-8"))
    )
    if not expected or any(count != 1 for count in expected.values()):
        raise ValueError("Required-case inventory is empty or duplicated")
    root = ET.parse(path).getroot()
    observed = Counter()
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise ValueError("Mandatory integrity acceptance contains failure/error/skip")
        observed[case.get("classname", "").rsplit(".", 1)[-1], case.get("name", "")] += 1
    if observed != expected:
        raise ValueError(
            f"Required cases diverge: missing={list((expected - observed).elements())}, unexpected={list((observed - expected).elements())}"
        )
    return sum(observed.values())


if __name__ == "__main__":
    count = check(Path(sys.argv[1]))
    sys.stdout.write(
        f"Verified {count} mandatory integrity cases, zero skips/failures/duplicates\n"
    )
