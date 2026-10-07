"""Cent-exact distribution across mutually exclusive outcomes."""

from decimal import Decimal, localcontext

from bancaemdia.domain.calculators.core import (
    FRACTION_UNIT,
    ONE,
    Calculation,
    allocation,
    amount_cents,
    as_percent,
    cents,
    quantize,
)
from bancaemdia.domain.calculators.probability import market


def distribute(values: list[tuple[str, str]], total: int) -> Calculation:
    """Equalize gross returns, then report every rounded net scenario."""
    cents(total, "stake_total_centavos")
    with localcontext() as ctx:
        ctx.prec = 48
        legs, inverse_sum = market(values)
        stakes = allocation(total, [probability for _, _, probability in legs])
        scenarios = []
        profits = []
        for (name, price, _), stake in zip(legs, stakes, strict=True):
            gross = amount_cents(Decimal(stake) * price)
            profit = gross - total
            profits.append(profit)
            scenarios.append({
                "name": name,
                "odd": price,
                "stake_centavos": stake,
                "return_centavos": gross,
                "profit_centavos": profit,
            })
        minimum = min(profits)
        theoretical = inverse_sum < ONE
        guaranteed = theoretical and minimum > 0
        warnings = ["At least one rounded scenario is not profitable."] if minimum <= 0 else []
        if theoretical and not guaranteed:
            warnings.append("Theoretical arbitrage disappears after cent rounding.")
        return Calculation(
            {
                "scenarios": scenarios,
                "total_stake_centavos": total,
                "inverse_odds_sum": quantize(inverse_sum, FRACTION_UNIT),
                "minimum_profit_centavos": minimum,
                "minimum_roi_percentage": as_percent(Decimal(minimum) / Decimal(total)),
                "theoretical_arbitrage": theoretical,
                "guaranteed_profit": guaranteed,
            },
            "inverse_odds_equal_gross_largest_remainder",
            (
                "All mutually exclusive outcomes must be supplied; completeness cannot be verified from odds.",
                "Guaranteed profit assumes every quoted price is executable and one outcome wins.",
            ),
            tuple(warnings),
        )
