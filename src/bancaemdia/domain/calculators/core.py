"""Shared numerical contract for calculator inputs and outputs.

Decimal inputs are plain decimal strings (no exponent), at most 12 integer and
8 fractional digits. Work uses a request-local 48-digit context. Fractions are
published to 8 places, percentages to 6, and money to integer cents using
ROUND_HALF_UP. Quantities are capped before arithmetic to bound resource use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")
FRACTION_UNIT = Decimal("0.00000001")
PERCENT_UNIT = Decimal("0.000001")
MAX_CENTS = 1_000_000_000_000
MAX_SELECTIONS = 20
DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,8})?$")


class CalculatorInputError(ValueError):
    """A bounded, safe-to-expose contract violation."""


@dataclass(frozen=True)
class Calculation:
    data: dict[str, Any]
    method: str
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    precision: str = "fraction:8; percent:6; money:cent; intermediate:48"
    rounding: str = "ROUND_HALF_UP; allocation:largest_remainder_input_order"


def decimal_input(
    value: object,
    name: str,
    *,
    minimum: Decimal = ZERO,
    maximum: Decimal = Decimal("999999999999"),
    inclusive: bool = False,
) -> Decimal:
    if not isinstance(value, str) or len(value) > 22 or DECIMAL_PATTERN.fullmatch(value) is None:
        raise CalculatorInputError(f"{name}: use a bounded plain decimal string")
    result = Decimal(value)
    if (result < minimum if inclusive else result <= minimum) or result > maximum:
        raise CalculatorInputError(f"{name}: outside supported range")
    return result.normalize()


def odds(value: object) -> Decimal:
    return decimal_input(value, "odd", minimum=ONE, maximum=Decimal("1000000"))


def cents(value: object, name: str, *, positive: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_CENTS:
        raise CalculatorInputError(f"{name}: invalid cent amount")
    if positive and value == 0:
        raise CalculatorInputError(f"{name}: must be positive")
    return value


def percentage(value: object) -> Decimal:
    return decimal_input(value, "percentual", maximum=HUNDRED, inclusive=True)


def validate_names(names: list[str], *, minimum: int) -> None:
    if not minimum <= len(names) <= MAX_SELECTIONS:
        raise CalculatorInputError(f"selections: provide {minimum} to {MAX_SELECTIONS}")
    canonical = [name.strip().casefold() for name in names]
    if any(not name or len(name) > 80 or name != name.strip() for name in names) or len(
        set(canonical)
    ) != len(canonical):
        raise CalculatorInputError("selections: distinct, nonblank names required")


def quantize(value: Decimal, unit: Decimal) -> Decimal:
    return value.quantize(unit, rounding=ROUND_HALF_UP)


def as_percent(fraction: Decimal) -> Decimal:
    return quantize(fraction * HUNDRED, PERCENT_UNIT)


def amount_cents(value: Decimal) -> int:
    return int(value.quantize(ONE, rounding=ROUND_HALF_UP))


def allocation(total: int, weights: list[Decimal]) -> list[int]:
    """Hamilton allocation; tied remainders go to the earliest input position."""
    cents(total, "total")
    if not 1 <= len(weights) <= MAX_SELECTIONS or any(
        w <= ZERO or not w.is_finite() for w in weights
    ):
        raise CalculatorInputError("weights: positive finite values required")
    with localcontext() as ctx:
        ctx.prec = 48
        denominator = sum(weights, ZERO)
        shares = [Decimal(total) * weight / denominator for weight in weights]
        floors = [int(share.to_integral_value(rounding=ROUND_FLOOR)) for share in shares]
        missing = total - sum(floors)
        order = sorted(
            range(len(weights)), key=lambda index: (-(shares[index] - floors[index]), index)
        )
        for index in order[:missing]:
            floors[index] += 1
    return floors


def serialize(value: Any) -> Any:
    """Preserve exact decimals in JSON instead of FastAPI's binary float encoder."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return value
