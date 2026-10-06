# The Odds API free-tier coverage probe — 28 September 2026

This is **live availability reconnaissance**, not a historical dataset or calibration benchmark. The owner supplied an API key through a local Git-ignored file. The key, raw odds, event names and player names were neither printed nor saved. The [aggregate-only probe](probe_the_odds_api.py) used current `/events`, `/events/{id}/markets` and `/events/{id}/odds` routes. All runs together consumed **144 of 500 free credits**; 356 remained after the final response. Query time was approximately 21:00 UTC. Each row below is a separate probe; several rows reuse the same upcoming events and must **not** be added as independent games.

| Sport / region | Events queried | Market key(s) | Complete over/under lines | Bookmaker/selection ladders with ≥2 complete lines | Interpretation |
|---|---:|---|---:|---:|---|
| Soccer EPL + La Liga + Serie A / `eu` | 5 | `alternate_totals`, first/second-half variants | 287 | 40 | Full-game/period goal ladders are available in this sample. |
| Soccer EPL + La Liga + Serie A / `uk` | 5 | `alternate_totals`, first-half and lay variants | 206 | 48 | Goal ladders are available; lay offers must be modeled separately from standard bookmaker odds. |
| Soccer EPL + La Liga / `us` | 4 | `alternate_totals_corners` | 34 | 4 | Match corner ladders found, from one bookmaker per event in this sample. |
| Soccer EPL + La Liga / `us` | Same 4 | `alternate_team_totals_corners` | 21 | 6 | Team-corner ladders found; data appeared in 3 of 4 queried events. |
| Soccer EPL + La Liga / `us` | Same 4 | `alternate_totals_corners_h1` | 14 | 5 | First-half corner ladders found; data appeared in 3 of 4 queried events. |
| NBA / `us` | 2 | `alternate_totals`, `alternate_team_totals` | 54 | 2 | Match-total ladders found; only one complete line per team in the team-total probe. |
| NBA / `us` | Same 2 | `player_points`, `player_assists` | 6 | 0 | Main player lines paired; no multi-line ladder in this sample. Alternate points/assists/rebounds returned no paired lines. |
| MLB / `us` | 2 | Alternate match/team runs, selected inning periods | 239 | 27 | Several run-total ladders found; periods and team scope must stay separate. |
| MLB / `us` | Same 2 | `batter_hits`, `pitcher_strikeouts` | 97 | 0 | Main player lines paired; selected alternate batter/pitcher props returned no paired lines. |
| NHL / `us` | 1–2 | Alternate team totals by period | 39 | 11 | Team/period goal ladders found. |
| NHL / `us` | 2 | Selected player goals/shots/points | 0 | 0 | Market keys exist, but selected responses lacked complete paired lines. |

The soccer `us` market list exposed corner totals and player shots on target; `player_shots_on_target` returned no paired line in the four queried events. Neither `alternate_totals_cards` nor a throw-ins/laterals key appeared in the queried soccer event lists. The `eu` and `uk` event lists contained goal totals but no corners/cards/throw-ins for their sampled events. This is **not proof of general absence**: availability varies by bookmaker, region, event and time to kickoff. Market keys alone do not prove usable ladders.

The probe's `books` count sums bookmaker appearances across events. It is not a count of independent sources or a guarantee of uniform bookmaker coverage. A complete line means both over and under exist at a point for the same bookmaker/market/selection in one response. A ladder requires at least two such points. This screening does not establish source-level simultaneity beyond a single API response, settled outcomes, historical availability, fair odds, commercial reliability or calibration. Integer/Asian settlement has not been audited. The output was not retained as a versioned dataset; this report records only aggregate counts and parameters.

## Acquisition decision after the free probe

The Odds API is a **plausible historical pilot for match goals, corners, NBA/MLB/NHL totals**. Its current alternate player props frequently lacked both sides, so they require a different source or a separately justified model before inclusion. Throw-ins remain unverified and should be explicitly requested from any alternate provider. The [free and paid plan descriptions](https://the-odds-api.com/) both say “all betting markets”; buying the USD 30 plan would unlock history, **not establish a throw-ins market**. A USD 30 historical pilot can test whether the *same* ladder depth exists in old event snapshots, but 20,000 credits cover at most 2,000 one-market/one-region historical event-snapshot calls. The owner's target of 5,000–10,000 independent events across several market families cannot be satisfied by assuming that the USD 30 tier suffices. If a first historical sample is useful, cost a larger dataset only after measuring yield per credit, outcome linkage and the provider's allowed commercial use. No paid subscription was started in this research.
