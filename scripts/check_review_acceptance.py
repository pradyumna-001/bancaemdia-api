"""Required review acceptance must execute successfully without skips."""

import logging
import sys
import xml.etree.ElementTree as ET

for path in sys.argv[1:]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases or any(
        case.find(kind) is not None for case in cases for kind in ("failure", "error", "skipped")
    ):
        raise SystemExit(f"Incomplete review acceptance: {path}")
    logging.warning("%s: %s cases passed without skips", path, len(cases))
