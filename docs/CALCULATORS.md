# Standard calculator API (issue #103)

Nine authenticated, stateless POST routes live under `/api/v1/calculadoras`. Every number needed
for a calculation is supplied in the request. These routes never read a user's bets or bankroll,
write a financial record, or call an external provider. They use the existing authenticated API
rate bucket. Responses are `{data, method, precision, rounding, assumptions, warnings}`. Decimal
values in JSON are strings; monetary amounts are integer centavos. Validation failures use 422.

## Numeric policy

- Odds, weights, percentages and commission are plain decimal **strings**; JSON binary numbers,
  exponent notation, signs, NaN and infinities are rejected. At most 12 integral and 8 fractional
  digits are accepted, and odds are restricted to `(1, 1000000]`. Money is integer cents in
  `[0, 1000000000000]`; the field determines whether zero is allowed. Up to 20 selections are
  accepted and outcome names must be distinct. Input names are at most 80 characters.
- Arithmetic uses a request-local `Decimal` context with 48 significant digits. Output fractions
  use 8 fractional places, percentages use 6 and money is rounded to cents with `ROUND_HALF_UP`.
  No process-global decimal context is changed.
- Shared largest-remainder allocation first floors each exact cent share, then awards leftover
  cents by descending fractional remainder. Equal remainders go to earlier input positions. This
  permits a legitimate zero-cent leg and always reconciles to the requested total.
- Market fair probabilities use the same largest-remainder idea at 8 decimal places, so their
  published sum is exactly 1. The matching odds are based on the higher-precision normalized
  probabilities, then rounded to 8 places.

## Market and planning assumptions

The caller must supply every mutually exclusive outcome for fair market, RTP, surebet and
dutching. Distinct labels and a minimum of two outcomes are checked, but odds cannot prove a real
market is complete. No-vig proportional normalization removes a quoted margin; it does not predict
true probabilities. RTP is `1 / sum(1 / odd)` and bookmaker margin is `sum(1 / odd) - 1`;
arbitrage may produce RTP above 100%.

Surebet and dutching allocate the total by inverse-odds weights, round gross returns to cents and
report net profit per outcome. Surebet is guaranteed **under the stated market and execution
assumptions** only when the inverse-odds sum is below one and every rounded scenario profit is
positive. Dutching does not claim arbitrage. Stake splitter percentages must total 100; weights
must total 1 unless `normalize_weights=true`. Zero percentages are allowed, while weights must be
positive. Odds on split legs enable projected gross returns.

Live hedge v1 accepts only two mutually exclusive cash-stake outcomes. Commission percentage is
charged on the **winning leg's odds profit**; it does not reduce the returned stake and it is never
charged to the losing leg. `equalize_profit` chooses hedge stake
`original_stake * [1+(original_odd-1)*(1-commission)] /
[1+(opposing_odd-1)*(1-commission)]`. `protect_stake` chooses
`original_stake / [(opposing_odd-1)*(1-commission)]`, targeting zero net loss when the hedge wins,
including both stakes. Actual scenario profit is returned after the hedge stake and each winning
payout are rounded to cents. Residual loss, rounding difference and inability to protect **both**
outcomes are explicit. The two-way, cash-only contract rejects freebets, partial cashouts, Asian
pushes and multi-way markets. Bet Analytix's *Live odds to cover* example of original 1.50 at
100 yielding opposite 3.000 at 50 matches the equalized, break-even vector; this API additionally
accepts a quoted opposing odd and reports the actual outcomes.

Target profit divides target cents by `odd - 1` and reports the profit from the rounded stake,
including any shortfall. It excludes freebets, commission and multiples. Bankroll percentage has
direct mode (`bankroll_centavos` + `percentage`, 0–100) and inverse mode
(`bankroll_centavos` + `stake_centavos`, 0–bankroll); exactly one mode input is required.

The endpoints are pure analyses despite using POST. The API's primary/replica routing therefore
does not mark them as financial writes. If the pending billing read-only gate is integrated later,
it must allow this calculator prefix while continuing to block genuine financial mutations.
