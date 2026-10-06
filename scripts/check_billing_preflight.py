"""Assess a minimal private evidence manifest without network or database access."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from bancaemdia.domain.billing_preflight import (
    MAX_MANIFEST_BYTES,
    PreflightReport,
    assess_preflight,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    today = datetime.now(UTC).date()
    try:
        with args.manifest.open("rb") as stream:
            report = assess_preflight(stream.read(MAX_MANIFEST_BYTES + 1), today=today)
    except OSError:
        report = PreflightReport(
            state="INVALID_INPUT", checked_on=today, blocking_checks=["manifest_unavailable"]
        )
    sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    return {"INVALID_INPUT": 2, "INCOMPLETE": 1, "OWNER_REVIEW_REQUIRED": 0}[report.state]


if __name__ == "__main__":
    raise SystemExit(main())
