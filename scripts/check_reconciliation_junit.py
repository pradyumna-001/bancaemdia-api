"""The mandatory dependent suite must execute, not disappear from a green workflow."""

import sys
from xml.etree import ElementTree

root = ElementTree.parse(sys.argv[1]).getroot()
cases = root.findall(".//testcase")
acceptance = [case for case in cases if "reconciliation_acceptance" in case.get("classname", "")]
if len(acceptance) < 28 or any(
    case.find(kind) is not None for case in cases for kind in ("skipped", "failure", "error")
):
    raise SystemExit("Required real reconciliation acceptance missing, skipped or failed")
sys.stdout.write(
    f"{len(acceptance)} historical scenarios; {len(cases)} total; zero skips/failures\n"
)
