# Calculator API (issue #103)

Four authenticated, stateless POST routes live under `/api/v1/calculadoras`:

| Route | Purpose |
| --- | --- |
| `mercado-justo` | Remove the quoted margin from every outcome of one complete market. |
| `distribuir-entre-resultados` | Allocate a stake across mutually exclusive outcomes and show each rounded profit or loss, including whether the supplied odds form a surebet. |
| `cobertura-ao-vivo` | Calculate a two-way hedge that equalizes the two net results. |
| `percentual-banca` | Convert a bankroll percentage to a stake, or a stake to a bankroll percentage. |

Every input is supplied in the request. The routes do not read a user's bets or bankroll,
write a financial record, or call an external provider. They use the existing authenticated API
rate bucket. Responses are `{data, method, precision, rounding, assumptions, warnings}`.
Decimal values in JSON are strings; monetary amounts are integer centavos. Invalid inputs return
422.

## Numeric policy

- Odds, percentages and commission are plain decimal **strings**. JSON numbers, exponent
  notation, signs, NaN and infinities are rejected. At most 12 integral and 8 fractional digits
  are accepted, with odds in `(1, 1000000]`. Money is integer cents in
  `[0, 1000000000000]`; each field determines whether zero is allowed. Up to 20 outcomes are
  accepted, with distinct names of at most 80 characters.
- Arithmetic uses a request-local `Decimal` context with 48 significant digits. Output
  fractions use 8 fractional places, percentages use 6, and money uses `ROUND_HALF_UP` to
  integer cents. No process-global decimal context is changed.
- Distribution uses largest-remainder allocation: floor each exact share, then award remaining
  cents by descending fractional remainder and input order for ties. It permits a legitimate
  zero-cent outcome and always reconciles to the requested total.
- Fair market probabilities use the same reconciliation at 8 decimal places, so their
  published sum is exactly 1. Corresponding fair odds are calculated from higher-precision
  normalized probabilities and then rounded to 8 places.

## Market assumptions

For `mercado-justo` and `distribuir-entre-resultados`, the caller must supply every
mutually exclusive outcome of the same market. The API checks distinct names and at least two
outcomes, but odds alone cannot establish that the market is complete.

`mercado-justo` computes each raw implied probability as `1 / odd`, sums those values,
and divides each by the sum. It reports both the raw values and the normalized ones, the
corresponding fair odds and `overround = sum(1 / odd) - 1`. Proportional margin removal is
**not** an estimate of the true probability or a forecast for a neighboring line. That
separate research belongs to issue #104.

`distribuir-entre-resultados` uses inverse-odds weights to approximately equalize gross
returns and reports the actual cent-rounded net profit in each outcome. It flags theoretical
arbitrage when the inverse-odds sum is below 1, and flags guaranteed profit only when **every**
rounded scenario is positive. This guarantee assumes that all outcomes are exhaustive and all
quoted prices can actually be accepted. Otherwise the response shows the loss or break-even
case instead of calling it a surebet.

## Hedge and bankroll assumptions

`cobertura-ao-vivo` supports exactly two mutually exclusive cash-stake outcomes. It chooses
the hedge stake as
`original_stake * [1 + (original_odd - 1) * (1 - commission)] /
[1 + (opposing_odd - 1) * (1 - commission)]`, so net results are as close as cent rounding
allows. Commission is charged on the winning leg's odds profit, never on returned stake or the
losing leg. The response reports both outcomes, the rounded hedge stake, rounding difference
and any residual loss. The contract excludes freebets, partial cashout, Asian pushes and
multi-way markets. The break-even example of original 1.50 at 100 and opposite 3.00 at 50
remains a regression vector.

`percentual-banca` supports direct mode (`bankroll_centavos` and `percentage`, 0–100)
and inverse mode (`bankroll_centavos` and `stake_centavos`, 0–bankroll). Supply exactly
one mode input. A calculated stake is sizing information, not a win prediction.

The endpoints are pure analyses despite using POST. The API's primary/replica routing does
not mark them as financial writes.
