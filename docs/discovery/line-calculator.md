# Line calculator discovery (#104)

Status: **research in progress; no market approved**. This document does not authorize an API or a prediction model. The product owner wants the broadest defensible market coverage, can evaluate a paid source, and specifically wants estimates learned from large sets of real neighboring-line quotes, including throw-ins if available. A market called “other” may be catalogued, but cannot receive a generic price without validation.

## Exact product question

For soccer player shots, given only `over 4.5 @ 1.86`, what additional information is required to estimate a **fair** `over 6.5` price for the same player, match, period, and market rules? At minimum we need either contemporaneous neighboring over/under lines with a defensible vig-removal rule, or a separately validated market-specific outcome model with the event state needed to condition its prediction. We also need to know whether the input and target are pre-match or live, the bookmaker and capture time, and how pushes are settled. A quoted price at one line is not enough.

For a concrete counterexample, both distributions below have `P(X > 4.5) = 0.5`:

| Count distribution | `P(X > 6.5)` |
|---|---:|
| `P(X=4)=0.5`, `P(X=5)=0.5` | 0 |
| `P(X=4)=0.5`, `P(X=7)=0.5` | 0.5 |

Even if the `1.86` quote were already fair (it is generally not), it would not identify the probability at 6.5. The first product response for such an input is `insufficient_data`, not an extrapolated odd.

## Two distinct interpretations of “fair”

1. **Market-implied, no-vig price**: remove bookmaker margin from a *complete simultaneous market* and interpolate its cross-line survival curve. For a half line, if both over and under odds are observed at the same bookmaker and time, use `q_over = (1/O_over) / ((1/O_over) + (1/O_under))`; `q_under = 1 - q_over`. An alternative de-vig rule must be benchmarked, not silently mixed. Separate bookmakers may be compared only after removing each book's margin. A fair decimal odd is `1/q` for a no-push binary bet. This is an estimate of the market's belief, not proof of true event probability.
2. **Outcome-calibrated price**: estimate the actual win/loss/push frequencies on held-out events. This needs verified settled outcomes and event-disjoint temporal validation. The two interpretations must be labelled in any future output; an accurate imitation of a bookmaker is not evidence of outcome calibration.

For an integer line there are three outcomes: win, push, loss. `over` and `under` win probabilities do **not** add to one; their sum is `1 - P(push)`. Decimal fair odds for a stake refunded on push satisfy `O_over = 1 + P(loss_over)/P(win_over)` and the analogous under formula. Two quoted integer-side odds alone do not identify push mass. A quarter Asian line splits the stake across adjacent half and integer lines; price it from the full outcome distribution and settlement convention. Reject requests with ambiguous rules or missing push data. Across equal market definitions, `P(over x)` must be non-increasing in `x` and `P(under x)` non-decreasing. Never “correct” incompatible quotes by hiding the conflict.

## Market taxonomy and scope

Approval is per `(sport, metric, participant scope, period, settlement rule, pre/live mode)` family, not per name substring. Initial soccer candidates are match/team goals, corners, cards, shots, shots on target, fouls, offsides, **throw-ins/laterals**, and player shots, shots on target, goals, assists, fouls, cards, saves. Hockey, basketball, baseball and other sports require separate metric and settlement mappings. Player propositions need stable, non-personal player identifiers and participation/void rules. “Other” is an **unsupported catalogue bucket** until sufficient same-definition observations and outcomes justify a model. A full-time total cannot train a first-half or team-specific target.

The user example “laterals 38.5 @ 1.8 to 40.5 @ 2.0” might describe prices on simultaneous alternate lines or line movement over time. These are different datasets. A timestamped progression by itself cannot identify the cross-line curve: the match state, lineup, score, and market view can change between quotes. Acquire simultaneous ladders for curve estimation and multiple snapshots of each event only for freshness/live-drift and line-movement studies.

## Source reconnaissance (28 September 2026)

| Source | What is documented | Critical gap / decision |
|---|---|
| [The Odds API](https://the-odds-api.com/liveapi/guides/v4/) | Historical **event** odds and alternate markets, with 5-minute historical snapshots since May 2023; paid access. [Market list](https://the-odds-api.com/sports-odds-data/betting-markets.html) includes alternate soccer corners/cards and some player props. [Terms](https://the-odds-api.com/terms-and-conditions.html) allow derived analytics and model training, subject to raw-feed redistribution restrictions. | Best documented low-cost **pilot candidate** for multiple sports. Verify that the actual response supplies both sides and multiple lines for each target market. Throw-ins are not listed in the published soccer keys, so coverage is unproven. Check exact commercial terms for planned display. [Published plans](https://the-odds-api.com/) start at USD 30/month; historical event requests consume credits by market/region. |
| [OpticOdds](https://developer.opticodds.com/docs/odds-api-getting-started-guide) | Broad sportsbook/sport catalogue, market discovery endpoint and historical odds. | [Documented historical endpoint](https://developer.opticodds.com/reference/get_fixtures-odds-historical) says a rolling two-month window and separately permitted time series. Ask sales for a representative export, retention, throw-ins coverage, licensing and quote; marketing “full history” is not enough. |
| [Betfair historical data](https://historicdata.betfair.com/) | Free Basic and paid Advanced/Pro Exchange files; historical API requires purchased downloads and session access ([developer support](https://support.developer.betfair.com/hc/en-us/articles/12859956891932-How-Can-I-Make-HTTP-Requests-to-the-Historical-Data-API)). | Portal terms limit ordinary use to personal/internal purposes. A commercial product requires a separate license. Exchange market availability and throw-ins are unverified. |
| [SharpAPI public sample](https://github.com/Sharp-API/SharpAPI-Sample-Data) | CC BY 4.0 static World Cup odds snapshot, suitable for inspecting field shapes and audit code. | Only a snapshot, principally two actual matches in its conventional match rows; no useful outcome-calibration set. It has no throw-ins market. Do not interpret 6,132 rows as 6,132 independent events. |

Source ranking is provisional: The Odds API for an inexpensive documented pilot; OpticOdds if its export proves broader depth (especially throw-ins); Betfair only after a suitable commercial license and coverage check. No subscription or credential has been obtained. The user has offered to **evaluate** a paid source; that is not a completed purchase or dataset approval.

The Odds API's [documented historical event cost](https://the-odds-api.com/liveapi/guides/v4/) is `10 × returned markets × requested regions` credits per event/snapshot. At the [published prices](https://the-odds-api.com/) on this research date, USD 30/month buys 20,000 credits (at most 2,000 one-market, one-region event/snapshot calls); USD 59 buys 100,000; USD 119 buys 5 million. These are **call ceilings, not guaranteed usable events**. For example, 5,000 events × 3 markets × 1 region × 1 snapshot would cost about 150,000 credits if all three markets return data. A small plan is appropriate only for a coverage probe; a full multi-family study needs a costed acquisition plan after a real response sample confirms ladder depth and result availability.

## Data acquisition and provenance gate

The sanitized observation contract is [`research/line-calculator/schema.json`](../../research/line-calculator/schema.json). A source-provided export must carry source/version/license, stable event and selection IDs, snapshot ID and UTC time, sport/league, bookmaker, exact metric/scope/period, line/unit/side/decimal odd, market status, settlement rule, pre/live state, and eventually verified final metric. Live records additionally need clock, score and metric-to-date, plus sport-specific state where relevant. Store credentials only in a secret manager or environment variable. Exclude account data, names, screenshots and personal betting history from the research export; pseudonymize player IDs if needed while retaining stable join keys. Record explicit product-owner consent, allowed commercial use, allowed derived-data storage, retention and redistribution limits before importing an owner-provided sample.

Dataset manifest must include provider contract/version, extraction window, markets, filters, timezone, SHA-256 of immutable export, parser revision, event count, snapshot count, paired complete-line count, event result count and exclusions. A nominal 5,000–10,000 **rows** is insufficient: seek thousands of independent settled events and enough multi-line snapshots **per approved family**, with both sides. Prove that multiple quotes from the same event, player and bookmaker are not counted as independent validation cases. Report missingness, bookmaker skew, market-definition conflicts and source changes.

## Proposed empirical benchmark

Pre-register eligible families and splits before fitting. Separate train/validation/test by **event ID** and calendar time (e.g. earlier events for train, later for validation, latest for locked test); group all player props and snapshots of one event together. Repeatedly backtest on forward time blocks and leagues. Fit de-vig choice and model hyperparameters on training/validation only. Never let final score, closing quote, or future game state enter a prediction made at an earlier time.

Compare, per family and pre/live mode:

- A quote-only **no-estimate** control for contexts without two usable neighboring complete lines; this establishes honest coverage.
- Direct monotone interpolation of simultaneous no-vig half-line quotes, with no extrapolation beyond observed bracket. Price intervals from bookmaker spread and source latency.
- A non-parametric/ordinal empirical survival curve conditioned on supported context, with monotonicity and sparse-data regularization.
- Market-appropriate distributional candidates (including overdispersed counts when warranted), calibrated on outcomes. Poisson is neither the default nor privileged baseline; any use requires empirical superiority and adequate tail calibration.

Measure: calibration plots and expected calibration error by probability bin, log loss and Brier on **binary half-line settlements**, win/push/loss or realized-return scoring for integer/Asian lines, absolute implied-probability and decimal-price error against held-out complete line quotes, coverage/abstention, monotonicity violations, and drift by family/league/bookmaker/time-to-start. Report event-level bootstrap confidence intervals and compare to no-vig bookmaker interpolation and simple empirical frequencies when scientifically valid. Avoid comparing decimal-odd errors without a bounded probability range because extreme longshots dominate. The model must beat a pre-registered baseline with confidence intervals and have acceptable calibration and coverage across the claimed operating range; numerical thresholds require owner approval **before** the locked test.

Sensitivity probes must vary margin-removal method, quote-age tolerance, one-sided/missing neighbors, line distance, bookmaker mix, low-liquidity books, live clock/score drift, player participation, and mismatched settlement definitions. Out-of-range, stale, suspended, conflicting or incomparable markets must abstain. Report per-family confidence and unsupported slices instead of a pooled success percentage.

## Proposed future contract (not implemented)

Input: event/participant, sport and exact metric/scope/period/settlement, source bookmaker and timestamp/snapshot, requested line and side, pre/live state, contemporaneous neighbor quotes if present. Output on success: interpretation (`market_implied` or `outcome_calibrated`), win/push/loss probabilities as applicable, fair decimal odd, observed bracket, method/model+dataset versions, independent event and paired-ladder support counts, uncertainty interval, supported-range/coverage flag, source age and limitations. Errors: `insufficient_data`, `unsupported_market`, `stale_quote`, `incompatible_market_definition`, `outside_supported_range`, `push_data_missing`, `market_suspended`. There must be no fabricated number when one line fails to identify the target.

## Decision status

See [ADR 025](../adrs/025-line-calculator-model-decision.md). All families remain **No-Go for implementation** pending licensed owner-approved sample, per-family benchmark/calibration and explicit owner/admin approval of both dataset and method. Only then may a separate implementation issue be proposed.
