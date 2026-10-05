"""Low-credit, aggregate-only live market coverage probe for The Odds API.

Read THE_ODDS_API_KEY from the environment. The free plan cannot fetch history,
so this script only checks current market availability and alternate-line depth.
It does not save or print raw prices, event names, player names, or the API key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

DEFAULT_SPORTS = (
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_brazil_campeonato",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
)


def fetch(path: str, key: str, **query: str) -> tuple[object, int, int | None]:
    params = urlencode({"apiKey": key, **query})
    url = f"https://api.the-odds-api.com/v4/{path}?{params}"
    try:
        with urlopen(url, timeout=20) as response:
            data = json.load(response)
            cost = int(response.headers.get("x-requests-last", "0"))
            remaining_raw = response.headers.get("x-requests-remaining")
            remaining = int(remaining_raw) if remaining_raw is not None else None
            return data, cost, remaining
    except HTTPError as exc:
        # Do not print the URL, response body or exception: they may contain the key.
        raise RuntimeError(f"The Odds API returned HTTP {exc.code}") from None
    except URLError:
        raise RuntimeError("The Odds API connection failed") from None


def outcome_ladders(event_odds: dict) -> dict[str, dict[str, int]]:
    """Count complete two-sided lines grouped by bookmaker and selection."""
    groups: dict[tuple[str, str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    books_by_market: dict[str, set[str]] = defaultdict(set)
    for bookmaker in event_odds.get("bookmakers", []):
        book_key = bookmaker.get("key", "")
        for market in bookmaker.get("markets", []):
            market_key = market.get("key", "")
            books_by_market[market_key].add(book_key)
            for outcome in market.get("outcomes", []):
                side = outcome.get("name", "").lower()
                point = outcome.get("point")
                if side not in {"over", "under"} or point is None:
                    continue
                selection = outcome.get("description") or "total"
                groups[market_key, book_key, selection][str(point)].add(side)

    summary: dict[str, dict[str, int]] = {
        key: {"books": len(books), "paired_lines": 0, "multi_line_ladders": 0}
        for key, books in books_by_market.items()
    }
    for (market_key, _, _), lines in groups.items():
        paired = sum({"over", "under"} <= sides for sides in lines.values())
        summary[market_key]["paired_lines"] += paired
        if paired >= 2:
            summary[market_key]["multi_line_ladders"] += 1
    return summary


def priority(market: str) -> tuple[int, str]:
    if "throw" in market or "lateral" in market:
        return 0, market
    if "alternate" in market and ("total" in market or "corner" in market):
        return 1, market
    if "alternate" in market:
        return 2, market
    if "total" in market or "player" in market:
        return 3, market
    return 4, market


def run(args: argparse.Namespace, key: str) -> dict:
    active_sports_data, _, remaining = fetch("sports", key)
    if not isinstance(active_sports_data, list):
        raise RuntimeError("Unexpected sports response")
    active_sports = {sport["key"] for sport in active_sports_data if sport.get("active")}
    spent = 0
    sports_result: dict[str, object] = {}
    result: dict[str, object] = {
        "note": "Current availability only; no historical or outcome validation",
        "credits_spent": 0,
        "credits_remaining": remaining,
        "sports": sports_result,
    }
    for sport in args.sports.split(","):
        sport = sport.strip()
        if sport not in active_sports:
            sports_result[sport] = {"status": "inactive_or_unknown"}
            continue
        regions = (
            ("eu" if sport.startswith("soccer_") else "us")
            if args.regions == "auto"
            else args.regions
        )
        regions_count = len(regions.split(","))
        events_data, _, remaining = fetch(f"sports/{quote(sport)}/events", key)
        if not isinstance(events_data, list):
            raise RuntimeError("Unexpected events response")
        events = sorted(events_data, key=lambda event: event.get("commence_time", ""))
        sport_summary: dict[str, object] = {
            "events_seen": 0,
            "available_market_keys": [],
            "quoted_markets": {},
        }
        seen_keys: set[str] = set()
        market_summary: dict[str, dict[str, int]] = defaultdict(
            lambda: {"events": 0, "books": 0, "paired_lines": 0, "multi_line_ladders": 0}
        )
        for event in events[: args.events_per_sport]:
            if spent + 1 > args.max_credits or remaining == 0:
                break
            event_id = quote(event["id"])
            markets_data, cost, remaining = fetch(
                f"sports/{quote(sport)}/events/{event_id}/markets",
                key,
                regions=regions,
            )
            spent += cost
            sport_summary["events_seen"] = int(sport_summary["events_seen"]) + 1
            if not isinstance(markets_data, dict):
                raise RuntimeError("Unexpected market list response")
            available = {
                market["key"]
                for book in markets_data.get("bookmakers", [])
                for market in book.get("markets", [])
                if market.get("key")
            }
            seen_keys.update(available)
            if args.markets:
                requested = [market.strip() for market in args.markets.split(",")]
                selected = [market for market in requested if market in available][
                    : args.markets_per_event
                ]
            else:
                candidates = [market for market in available if priority(market)[0] < 4]
                selected = sorted(candidates, key=priority)[: args.markets_per_event]
            max_cost = len(selected) * regions_count
            if not selected or spent + max_cost > args.max_credits:
                continue
            if remaining is not None and remaining < max_cost:
                continue
            odds_data, cost, remaining = fetch(
                f"sports/{quote(sport)}/events/{event_id}/odds",
                key,
                regions=regions,
                markets=",".join(selected),
                oddsFormat="decimal",
            )
            spent += cost
            if not isinstance(odds_data, dict):
                raise RuntimeError("Unexpected odds response")
            for market, counts in outcome_ladders(odds_data).items():
                aggregate = market_summary[market]
                aggregate["events"] += 1
                for name in ("books", "paired_lines", "multi_line_ladders"):
                    aggregate[name] += counts[name]
        sport_summary["available_market_keys"] = sorted(seen_keys)
        sport_summary["quoted_markets"] = dict(sorted(market_summary.items()))
        sports_result[sport] = sport_summary
        if spent >= args.max_credits or remaining == 0:
            break
    result["credits_spent"] = spent
    result["credits_remaining"] = remaining
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sports", default=",".join(DEFAULT_SPORTS))
    parser.add_argument("--regions", default="auto")
    parser.add_argument("--markets", default="")
    parser.add_argument("--events-per-sport", type=int, default=2)
    parser.add_argument("--markets-per-event", type=int, default=5)
    parser.add_argument("--max-credits", type=int, default=30)
    options = parser.parse_args()
    if min(options.events_per_sport, options.markets_per_event, options.max_credits) < 1:
        parser.error("limits must be positive integers")
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        parser.error("THE_ODDS_API_KEY is not set in the environment")
    try:
        sys.stdout.write(json.dumps(run(options, api_key), indent=2) + "\n")
    except RuntimeError as error:
        parser.exit(1, f"{error}\n")
