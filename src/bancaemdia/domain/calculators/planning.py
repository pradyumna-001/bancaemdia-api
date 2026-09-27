"""Stake targets and bankroll sizing; live hedge is added after objective review."""

from decimal import Decimal, localcontext

from bancaemdia.domain.calculators.core import (
    HUNDRED,
    ONE,
    PERCENT_UNIT,
    Calculation,
    CalculatorInputError,
    amount_cents,
    as_percent,
    cents,
    odds,
    percentage,
    quantize,
)


def target_profit(price: str, target_cents: int) -> Calculation:
    cents(target_cents, "lucro_alvo_centavos")
    with localcontext() as ctx:
        ctx.prec = 48
        odd = odds(price)
        ideal = Decimal(target_cents) / (odd - ONE)
        rounded = amount_cents(ideal)
        if rounded > 1_000_000_000_000:
            raise CalculatorInputError("stake: exceeds supported limit")
        realized = amount_cents(Decimal(rounded) * (odd - ONE))
        return Calculation(
            {
                "ideal_stake_centavos": quantize(ideal, PERCENT_UNIT),
                "stake_centavos": rounded,
                "target_profit_centavos": target_cents,
                "realized_profit_centavos": realized,
                "difference_centavos": realized - target_cents,
            },
            "target_profit_divided_by_net_odds",
            ("Cash stake and single decimal odd; freebets, commission and multiples excluded.",),
            ("Rounded stake falls short of target.",) if realized < target_cents else (),
        )


def bankroll_percent(
    bankroll: int, *, percent: str | None = None, stake_cents: int | None = None
) -> Calculation:
    cents(bankroll, "banca_centavos")
    if (percent is None) == (stake_cents is None):
        raise CalculatorInputError("provide exactly one of percentual or stake_centavos")
    with localcontext() as ctx:
        ctx.prec = 48
        if percent is not None:
            value = percentage(percent)
            ideal = Decimal(bankroll) * value / HUNDRED
            rounded = amount_cents(ideal)
            return Calculation(
                {
                    "mode": "direct",
                    "banca_centavos": bankroll,
                    "percentage": value,
                    "ideal_stake_centavos": quantize(ideal, PERCENT_UNIT),
                    "stake_centavos": rounded,
                },
                "bankroll_times_percentage",
            )
        assert stake_cents is not None
        cents(stake_cents, "stake_centavos", positive=False)
        if stake_cents > bankroll:
            raise CalculatorInputError("stake_centavos: cannot exceed bankroll")
        ratio = Decimal(stake_cents) / Decimal(bankroll)
        return Calculation(
            {
                "mode": "inverse",
                "banca_centavos": bankroll,
                "stake_centavos": stake_cents,
                "percentage": as_percent(ratio),
                "exact_percentage": quantize(ratio * HUNDRED, PERCENT_UNIT),
            },
            "stake_divided_by_bankroll",
        )


def live_hedge(
    original_stake: int,
    original_price: str,
    opposing_price: str,
    objective: str,
    commission: str = "0",
) -> Calculation:
    """Commission is charged on winning net odds profit, never on returned stake."""
    cents(original_stake, "stake_original_centavos")
    if objective not in {"equalize_profit", "protect_stake"}:
        raise CalculatorInputError("objective: unsupported")
    with localcontext() as ctx:
        ctx.prec = 48
        first = odds(original_price)
        second = odds(opposing_price)
        fee = percentage(commission) / HUNDRED
        if fee == ONE:
            raise CalculatorInputError("commission: must be below 100 percent")
        first_effective = ONE + (first - ONE) * (ONE - fee)
        second_effective = ONE + (second - ONE) * (ONE - fee)
        ideal = (
            Decimal(original_stake) * first_effective / second_effective
            if objective == "equalize_profit"
            else Decimal(original_stake) / (second_effective - ONE)
        )
        rounded = amount_cents(ideal)
        if rounded > 1_000_000_000_000:
            raise CalculatorInputError("hedge stake: exceeds supported limit")
        original_return = amount_cents(Decimal(original_stake) * first_effective)
        hedge_return = amount_cents(Decimal(rounded) * second_effective)
        total = original_stake + rounded
        original_profit = original_return - total
        hedge_profit = hedge_return - total
        warnings = []
        if min(original_profit, hedge_profit) < 0:
            warnings.append("At least one realized scenario has a residual loss.")
        if objective == "protect_stake" and original_profit < 0:
            warnings.append(
                "Protecting the hedge-win scenario cannot protect both outcomes at these odds."
            )
        if rounded == 0:
            warnings.append("The ideal hedge rounds to zero cents.")
        return Calculation(
            {
                "objective": objective,
                "original_odd": first,
                "opposing_odd": second,
                "commission_percentage": percentage(commission),
                "original_stake_centavos": original_stake,
                "ideal_hedge_stake_centavos": quantize(ideal, PERCENT_UNIT),
                "hedge_stake_centavos": rounded,
                "rounding_difference_centavos": quantize(Decimal(rounded) - ideal, PERCENT_UNIT),
                "original_wins": {
                    "return_centavos": original_return,
                    "profit_centavos": original_profit,
                },
                "hedge_wins": {"return_centavos": hedge_return, "profit_centavos": hedge_profit},
                "both_outcomes_protected": min(original_profit, hedge_profit) >= 0,
            },
            "equal_net_profit" if objective == "equalize_profit" else "zero_net_loss_if_hedge_wins",
            (
                "Exactly two mutually exclusive cash-stake outcomes; no freebet, partial cashout or Asian push.",
                "Commission applies only to the winning leg's odds profit, before cent rounding.",
                "Profit subtracts both stakes; quoted prices must be executable.",
            ),
            tuple(warnings),
        )
