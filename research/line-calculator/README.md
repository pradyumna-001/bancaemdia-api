# Reproducible data audit for issue #104

This directory contains a **data contract and quality audit**, not a trained model. The checked-in fixture is explicitly synthetic. No API key, paid export or personal account data is stored here.

## Public sample and provenance

The [SharpAPI Sample Data repository](https://github.com/Sharp-API/SharpAPI-Sample-Data) publishes the two CSV snapshots below under CC BY 4.0. Attribution: SharpAPI, *SharpAPI Sample Data*, repository revision `cddb647cc9d06bd244cb8b72111cb39bd5587478`. The files were inspected on 28 September 2026 and are **not** checked into this repository. Downloading them is optional; the documented revision and SHA-256 make the audit repeatable.

```powershell
$sourceRevision = 'cddb647cc9d06bd244cb8b72111cb39bd5587478'
$sampleRoot = Join-Path $env:TEMP 'bancaemdia-line-research'
New-Item -ItemType Directory -Force -Path $sampleRoot | Out-Null
Invoke-WebRequest "https://raw.githubusercontent.com/Sharp-API/SharpAPI-Sample-Data/$sourceRevision/data/worldcup_2026_odds_snapshot.csv" -OutFile (Join-Path $sampleRoot 'worldcup.csv')
Invoke-WebRequest "https://raw.githubusercontent.com/Sharp-API/SharpAPI-Sample-Data/$sourceRevision/data/mlb_odds_snapshot.csv" -OutFile (Join-Path $sampleRoot 'mlb.csv')
python research/line-calculator/audit_snapshot.py (Join-Path $sampleRoot 'worldcup.csv')
python research/line-calculator/audit_snapshot.py (Join-Path $sampleRoot 'mlb.csv')
```

| Static snapshot | SHA-256 | Rows | Source event IDs | Quoted lines with both over/under | Groups with ≥2 paired lines |
|---|---|---:|---:|---:|---:|
| World Cup | `ed742facd1ecb9f533da7a6960fc78caea696c19849e2d33cc17bf74ce51815d` | 6,132 | 250 | 122 | 4 |
| MLB | `3fe2bcb54448816d22154a394dc9e86a02e31931e08d543127288c1bed9788f9` | 3,673 | 319 | 0 | 0 |

World Cup paired lines: 56 `player_shots_on_target`, 37 `player_fouls`, 13 `player_shots_attempted`, 10 `player_goals`, 3 `player_goals_+_assists`, 2 `player_saves`, 1 `player_assists`. Only `player_goals` and `player_shots_on_target` have two groups each with at least two paired lines; the other market slugs have no usable multi-line groups. The 250 source event IDs include futures and therefore are **not** 250 independent settled games. Conventional match rows concentrate in two fixtures. The CSV has no verified result labels or repeated time history. It cannot support a calibration, price-error, or 5,000-event claim. The audit groups a single file without respecting subsecond capture differences; a paid longitudinal feed must use explicit source snapshot IDs and timestamp tolerance, or the number of truly simultaneous ladders may be smaller.

## Intake checklist for a paid/owner sample

1. Owner approves provider and research use, commercial license, retention, and permitted derivative display. Record agreement reference; put the API key in an environment variable/secret manager only.
2. Export an **immutable** pre-match sample spanning several leagues, bookmakers and months, with full alternate line ladders, both sides and final metric results. Separately collect live data with clock/score/stat state if live coverage is desired. Include throw-ins in the provider's sample request; a published market name alone is not proof of history or ladder depth.
3. Map each record into [`schema.json`](schema.json) without human account identifiers. Preserve raw source IDs in a restricted dataset outside Git and a sanitized research export. Do not join by display names.
4. Publish a manifest with SHA-256, time range, family mapping, counts of independent settled events, snapshots and paired ladders, result completeness and exclusions. Freeze train/validation/test by event and time before model comparison.
5. Run the [discovery benchmark protocol](../../docs/discovery/line-calculator.md), publish per-family metrics and confidence intervals, then update [ADR 025](../../docs/adrs/025-line-calculator-model-decision.md) for owner/admin approval.

The audit script accepts the public sample's CSV columns. It is intentionally strict about needing `event_id`, `sportsbook`, `market_type`, `selection`, `selection_type`, `line` and `timestamp`; it does not pretend to normalize an arbitrary provider feed. A paid-source adapter needs a separate, reviewed field map and settlement-rule validation.
