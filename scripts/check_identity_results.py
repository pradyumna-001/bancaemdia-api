"""Fail the delivery if any mandatory real-issuer/database acceptance case did not pass."""

import sys
from pathlib import Path
from xml.etree import ElementTree


def main(path: Path) -> None:
    cases = ElementTree.parse(path).getroot().findall(".//testcase")
    required = {
        "test_real_registration_refresh_recovery_isolation_and_revocation",
        "test_repeated_concurrent_provision_has_one_proven_numeric_user",
        "test_ordinary_api_role_has_no_private_identity_or_token_access",
    }
    if not required <= {case.attrib["name"] for case in cases}:
        raise SystemExit("Mandatory identity journey/database proof is missing")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise SystemExit("Identity acceptance requires every case passing with zero skips")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
