"""Fail mandatory acceptance if no scenario ran, or any scenario was skipped."""

import sys
from xml.etree import ElementTree

for filename in sys.argv[1:]:
    root = ElementTree.parse(filename).getroot()
    cases = root.findall(".//testcase")
    if not cases or any(case.find("skipped") is not None for case in cases):
        raise SystemExit(f"{filename}: mandatory acceptance is empty or contains skips")
    sys.stdout.write(f"{filename}: {len(cases)} scenarios, zero skips\n")
