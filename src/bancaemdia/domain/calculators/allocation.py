"""Cent-exact stake allocations and realized market scenarios."""

from decimal import Decimal, localcontext

from bancaemdia.domain.calculators.core import (
    FRACTION_UNIT,
    HUNDRED,
    ONE,
    ZERO,
    Calculation,
    CalculatorInputError,
    allocation,
    amount_cents,
    as_percent,
    cents,
    decimal_input,
    odds,
    quantize,
    validate_names,
)
from bancaemdia.domain.calculators.probability import market


def _equal_returns(
    values: list[tuple[str, str]], total: int
) -> tuple[list[dict[str, object]], Decimal]:
    cents(total, "stake_total_centavos")
    legs, inverse_sum = market(values)
    stakes = allocation(total, [probability for _, _, probability in legs])
    results = []
    for (name, price, _), stake in zip(legs, stakes, strict=True):
        gross = amount_cents(Decimal(stake) * price)
        results.append({
            "name": name,
            "odd": price,
            "stake_centavos": stake,
            "return_centavos": gross,
            "profit_centavos": gross - total,
        })
    return results, inverse_sum


def dutching(values: list[tuple[str, str]], total: int) -> Calculation:
    with localcontext() as ctx:
        ctx.prec = 48
        scenarios, inverse_sum = _equal_returns(values, total)
        profits: list[int] = []
        for leg in scenarios:
            profit = leg["profit_centavos"]
            assert isinstance(profit, int)
            profits.append(profit)
        minimum = min(profits)
        return Calculation(
            {
                "scenarios": scenarios,
                "total_stake_centavos": total,
                "inverse_odds_sum": quantize(inverse_sum, FRACTION_UNIT),
                "minimum_profit_centavos": minimum,
            },
            "inverse_odds_equal_gross_largest_remainder",
            ("All mutually exclusive outcomes must be supplied.",),
            ("A rounded scenario loses money or breaks even; dutching is not a surebet.",)
            if minimum <= 0
            else (),
        )


def surebet(values: list[tuple[str, str]], total: int) -> Calculation:
    result = dutching(values, total)
    _, exact_inverse_sum = market(values)
    minimum = result.data["minimum_profit_centavos"]
    assert isinstance(minimum, int)
    theoretical = exact_inverse_sum < ONE
    guaranteed = theoretical and minimum > 0
    warnings = ["At least one rounded scenario is not profitable."] if minimum <= 0 else []
    if theoretical and not guaranteed:
        warnings.append("Theoretical arbitrage disappears after cent rounding.")
    return Calculation(
        {
            **result.data,
            "theoretical_arbitrage": theoretical,
            "guaranteed_profit": guaranteed,
            "minimum_roi_percentage": as_percent(Decimal(minimum) / Decimal(total)),
        },
        "inverse_odds_equal_gross_largest_remainder",
        (
            "All mutually exclusive outcomes must be supplied; completeness cannot be verified from odds.",
            "Guaranteed profit assumes every quoted price is executable and one outcome wins.",
        ),
        tuple(warnings),
    )


def split(
    total: int, items: list[tuple[str, str, str | None]], mode: str, normalize_weights: bool = False
) -> Calculation:
    cents(total, "stake_total_centavos")
    validate_names([name for name, _, _ in items], minimum=1)
    if mode not in {"percentages", "weights"} or (mode == "percentages" and normalize_weights):
        raise CalculatorInputError("mode: invalid normalization choice")
    with localcontext() as ctx:
        ctx.prec = 48
        weights = [
            decimal_input(weight, "weight", maximum=HUNDRED, inclusive=mode == "percentages")
            for _, weight, _ in items
        ]
        if mode == "weights" and any(weight <= ZERO for weight in weights):
            raise CalculatorInputError("weights: positive values required")
        target_sum = HUNDRED if mode == "percentages" else ONE
        if not normalize_weights and sum(weights, ZERO) != target_sum:
            raise CalculatorInputError(f"{mode}: values must sum to {target_sum}")
        stakes = (
            allocation(total, weights)
            if all(w > ZERO for w in weights)
            else _zero_weight_allocation(total, weights)
        )
        selections = []
        for (name, weight, price), stake in zip(items, stakes, strict=True):
            selections.append({
                "name": name,
                "weight": weight,
                "stake_centavos": stake,
                "projected_return_centavos": None
                if price is None
                else amount_cents(Decimal(stake) * odds(price)),
            })
        return Calculation(
            {"selections": selections, "total_stake_centavos": total},
            "largest_remainder_input_order",
            (
                "Weights are normalized only when normalize_weights is true.",
                "Equal fractional remainders are awarded in input order.",
            ),
        )


def _zero_weight_allocation(total: int, weights: list[Decimal]) -> list[int]:
    positive = [(index, weight) for index, weight in enumerate(weights) if weight > ZERO]
    if not positive:
        raise CalculatorInputError("percentages: at least one positive percentage required")
    allocated = allocation(total, [weight for _, weight in positive])
    result = [0] * len(weights)
    for (index, _), stake in zip(positive, allocated, strict=True):
        result[index] = stake
    return result
