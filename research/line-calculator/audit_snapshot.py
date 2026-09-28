"""Audit an odds CSV for simultaneous, paired neighboring-line evidence.

This is a data-quality audit, not a fair-odds estimator. It uses only Python's
standard library. Its grouping assumes a single snapshot; do not combine
different capture times in a longitudinal feed without an explicit tolerance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


REQUIRED = {
    "event_id",
    "sportsbook",
    "market_type",
    "selection",
    "selection_type",
    "line",
    "timestamp",
}


def audit(path: Path) -> dict:
    groups: dict[tuple[str, ...], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    rows = 0
    events: set[str] = set()
    books: set[str] = set()
    markets: set[str] = set()
    timestamps: set[str] = set()
    line_rows = 0
    invalid_odds = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            rows += 1
            events.add(row["event_id"])
            books.add(row["sportsbook"])
            markets.add(row["market_type"])
            timestamps.add(row["timestamp"])
            if row["line"]:
                line_rows += 1
            if "odds_decimal" in row:
                try:
                    if float(row["odds_decimal"]) <= 1:
                        invalid_odds += 1
                except (TypeError, ValueError):
                    invalid_odds += 1
            if row["line"] and row["selection_type"] in {"over", "under"}:
                # Ignore timestamp only because the public file is one capture.
                # Production data must use a defined same-time snapshot ID.
                key = (
                    row["event_id"],
                    row["sportsbook"],
                    row["market_type"],
                    row["selection"],
                )
                groups[key][row["line"]].add(row["selection_type"])

    paired_lines_by_market: Counter[str] = Counter()
    ladders_by_market: Counter[str] = Counter()
    for key, lines in groups.items():
        paired = sum({"over", "under"} <= sides for sides in lines.values())
        paired_lines_by_market[key[2]] += paired
        if paired >= 2:
            ladders_by_market[key[2]] += 1

    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": rows,
        "distinct_source_event_ids": len(events),
        "books": len(books),
        "market_slugs": len(markets),
        "timestamps": len(timestamps),
        "rows_with_line": line_rows,
        "invalid_decimal_odds": invalid_odds,
        "paired_lines": sum(paired_lines_by_market.values()),
        "ladders_with_two_or_more_paired_lines": sum(ladders_by_market.values()),
        "paired_lines_by_market": dict(sorted(paired_lines_by_market.items())),
        "ladders_by_market": dict(sorted(ladders_by_market.items())),
        "limitations": [
            "Source event IDs include futures; they are not independent settled matches.",
            "This single snapshot cannot measure temporal movement or outcome calibration.",
            "Pairing ignores subsecond timestamps; source-level simultaneity is unverified.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.csv_path), indent=2, ensure_ascii=False))
