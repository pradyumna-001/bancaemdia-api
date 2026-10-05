"""Probability and market calculations over the shared decimal core."""

from decimal import ROUND_FLOOR, Decimal, localcontext

from bancaemdia.domain.calculators.core import (
    FRACTION_UNIT,
    ONE,
    ZERO,
    Calculation,
    odds,
    quantize,
    validate_names,
)


def market(values: list[tuple[str, str]]) -> tuple[list[tuple[str, Decimal, Decimal]], Decimal]:
    validate_names([name for name, _ in values], minimum=2)
    with localcontext() as ctx:
        ctx.prec = 48
        legs = [(name, odds(price), ONE / odds(price)) for name, price in values]
        inverse_sum = sum((probability for _, _, probability in legs), ZERO)
    return legs, inverse_sum


def fair(values: list[tuple[str, str]]) -> Calculation:
    legs, inverse_sum = market(values)
    with localcontext() as ctx:
        ctx.prec = 48
        exact = [raw / inverse_sum for _, _, raw in legs]
        units = [
            int((prob / FRACTION_UNIT).to_integral_value(rounding=ROUND_FLOOR)) for prob in exact
        ]
        remainders = [
            (prob / FRACTION_UNIT) - unit for prob, unit in zip(exact, units, strict=True)
        ]
        remaining = 100_000_000 - sum(units)
        for index in sorted(range(len(units)), key=lambda i: (-remainders[i], i))[:remaining]:
            units[index] += 1
        outcomes = [
            {
                "name": name,
                "odd": price,
                "implied_probability": quantize(raw, FRACTION_UNIT),
                "fair_probability": Decimal(units[index]) * FRACTION_UNIT,
                "fair_odd": quantize(ONE / exact[index], FRACTION_UNIT),
            }
            for index, (name, price, raw) in enumerate(legs)
        ]
        return Calculation(
            {
                "outcomes": outcomes,
                "inverse_odds_sum": quantize(inverse_sum, FRACTION_UNIT),
                "overround": quantize(inverse_sum - ONE, FRACTION_UNIT),
            },
            "proportional_normalization_largest_remainder",
            (
                "All mutually exclusive outcomes must be supplied; completeness cannot be verified from odds.",
                "Margin removal is not a prediction of true probabilities.",
            ),
        )
