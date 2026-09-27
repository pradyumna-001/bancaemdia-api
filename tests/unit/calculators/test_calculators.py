"""Independent vectors and bounded randomized invariants for issue 103."""

import ast
import random
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from bancaemdia.domain.calculators import allocation as alloc
from bancaemdia.domain.calculators import planning, probability
from bancaemdia.domain.calculators.core import CalculatorInputError, allocation, decimal_input


def test_implied_and_market_vectors() -> None:
    assert probability.implied("2.0").data == probability.implied("2.00").data
    assert probability.implied("2.00").data == {
        "probability": Decimal("0.50000000"),
        "percentage": Decimal("50.000000"),
    }
    fair = probability.fair([("A", "2"), ("B", "2")]).data
    assert [leg["fair_probability"] for leg in fair["outcomes"]] == [
        Decimal("0.50000000"),
        Decimal("0.50000000"),
    ]
    assert fair["overround"] == Decimal("0E-8")
    three = probability.fair([("A", "3"), ("B", "3"), ("C", "3")]).data
    assert sum(leg["fair_probability"] for leg in three["outcomes"]) == 1
    assert three["outcomes"][0]["fair_probability"] == Decimal("0.33333334")
    assert probability.rtp([("A", "1.8"), ("B", "1.8")]).data["rtp_percentage"] == Decimal(
        "90.000000"
    )
    assert probability.rtp([("A", "2.1"), ("B", "2.1")]).data["rtp_percentage"] == Decimal(
        "105.000000"
    )
    assert probability.rtp([("A", "1.2"), ("B", "1.2")]).data[
        "bookmaker_margin_percentage"
    ] == Decimal("66.666667")


@pytest.mark.parametrize(
    "bad", ["1", "0", "NaN", "Infinity", "1e100000", "2.000000000", "9999999999999", "-2", "abc"]
)
def test_bad_odds_rejected(bad: str) -> None:
    with pytest.raises(CalculatorInputError):
        probability.implied(bad)


def test_market_structure_and_order() -> None:
    with pytest.raises(CalculatorInputError):
        probability.rtp([("A", "2")])
    with pytest.raises(CalculatorInputError):
        probability.fair([("A", "2"), ("A", "3")])
    with pytest.raises(CalculatorInputError):
        probability.fair([("A", "2"), ("a", "3")])
    with pytest.raises(CalculatorInputError):
        probability.fair([("A ", "2"), ("B", "3")])
    original = [("A", "1.8"), ("B", "3.2"), ("C", "5")]
    reordered = list(reversed(original))
    assert [leg["name"] for leg in probability.fair(reordered).data["outcomes"]] == ["C", "B", "A"]
    assert probability.implied("1000000").data["probability"] == Decimal("0.00000100")


def test_randomized_fair_probabilities_reconcile() -> None:
    rng = random.Random(103)
    for _ in range(200):
        prices = [
            (str(i), str(Decimal(rng.randint(101, 100_000)) / 100))
            for i in range(rng.randint(2, 20))
        ]
        first = probability.fair(prices).data
        second = probability.fair(prices).data
        assert first == second
        assert sum(item["fair_probability"] for item in first["outcomes"]) == Decimal("1")
        assert all(item["fair_probability"] > 0 for item in first["outcomes"])


def test_allocation_examples_and_properties() -> None:
    assert allocation(1, [Decimal("1"), Decimal("1")]) == [1, 0]
    assert allocation(5, [Decimal("1"), Decimal("1"), Decimal("1")]) == [2, 2, 1]
    rng = random.Random(103)
    for _ in range(500):
        total = rng.randint(1, 100_000)
        weights = [Decimal(rng.randint(1, 10_000)) for _ in range(rng.randint(1, 20))]
        first = allocation(total, weights)
        assert first == allocation(total, weights)
        assert all(stake >= 0 for stake in first)
        assert sum(first) == total


def test_surebet_and_dutching_realized_scenarios() -> None:
    bets = [("A", "2.10"), ("B", "2.10")]
    result = alloc.surebet(bets, 10_000).data
    assert [leg["stake_centavos"] for leg in result["scenarios"]] == [5_000, 5_000]
    assert [leg["profit_centavos"] for leg in result["scenarios"]] == [500, 500]
    assert result["minimum_profit_centavos"] == 500
    assert result["guaranteed_profit"] is True
    tiny = alloc.surebet(bets, 1).data
    assert tiny["theoretical_arbitrage"] is True
    assert tiny["guaranteed_profit"] is False
    losing = alloc.dutching([("A", "1.9"), ("B", "1.9")], 10_000)
    assert losing.data["minimum_profit_centavos"] == -500
    assert losing.warnings
    for total in range(1, 250):
        checked = alloc.surebet([("A", "2.05"), ("B", "2.10"), ("C", "7")], total).data
        profits = [leg["profit_centavos"] for leg in checked["scenarios"]]
        assert checked["minimum_profit_centavos"] == min(profits)
        assert checked["guaranteed_profit"] is False or all(profit > 0 for profit in profits)
        assert sum(leg["stake_centavos"] for leg in checked["scenarios"]) == total


def test_split_modes_and_remainders() -> None:
    result = alloc.split(1, [("A", "50", None), ("B", "50", "2")], "percentages")
    assert [item["stake_centavos"] for item in result.data["selections"]] == [1, 0]
    assert result.data["selections"][1]["projected_return_centavos"] == 0
    assert [
        item["stake_centavos"]
        for item in alloc.split(
            101,
            [("A", "1", None), ("B", "1", None)],
            "weights",
            True,
        ).data["selections"]
    ] == [51, 50]
    for args in [
        ([("A", "49", None), ("B", "50", None)], "percentages", False),
        ([("A", "1", None), ("B", "1", None)], "weights", False),
        ([("A", "0", None)], "weights", True),
    ]:
        with pytest.raises(CalculatorInputError):
            alloc.split(100, *args)


def test_hedge_vectors_commission_and_residuals() -> None:
    # Bet Analytix break-even vector: original 1.5 at 100, opposite 3 at 50.
    result = planning.live_hedge(10_000, "1.5", "3", "equalize_profit").data
    assert result["hedge_stake_centavos"] == 5_000
    assert result["original_wins"]["profit_centavos"] == 0
    assert result["hedge_wins"]["profit_centavos"] == 0
    protected = planning.live_hedge(10_000, "1.5", "2", "protect_stake", "10").data
    hedge = protected["hedge_stake_centavos"]
    expected_return = (Decimal(hedge) * Decimal("1.9")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    assert protected["hedge_wins"]["return_centavos"] == int(expected_return)
    assert protected["hedge_wins"]["profit_centavos"] == int(expected_return) - 10_000 - hedge
    assert protected["original_wins"]["profit_centavos"] < 0
    assert protected["both_outcomes_protected"] is False


def test_randomized_hedge_scenarios_recompute_from_reported_inputs() -> None:
    rng = random.Random(104)
    for _ in range(200):
        original_stake = rng.randint(1, 1_000_000)
        first = Decimal(rng.randint(101, 500)) / 100
        second = Decimal(rng.randint(101, 500)) / 100
        commission = Decimal(rng.randint(0, 50))
        objective = rng.choice(["equalize_profit", "protect_stake"])
        data = planning.live_hedge(
            original_stake, str(first), str(second), objective, str(commission)
        ).data
        hedge = data["hedge_stake_centavos"]
        total = original_stake + hedge
        factor = Decimal("1") - commission / 100
        original_return = (Decimal(original_stake) * (1 + (first - 1) * factor)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        hedge_return = (Decimal(hedge) * (1 + (second - 1) * factor)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        assert data["original_wins"]["profit_centavos"] == int(original_return) - total
        assert data["hedge_wins"]["profit_centavos"] == int(hedge_return) - total


def test_target_and_bankroll_boundaries() -> None:
    target = planning.target_profit("3", 101).data
    assert target["ideal_stake_centavos"] == Decimal("50.500000")
    assert target["stake_centavos"] == 51
    assert target["realized_profit_centavos"] == 102
    shortfall = planning.target_profit("4", 100)
    assert shortfall.data["stake_centavos"] == 33
    assert shortfall.data["realized_profit_centavos"] == 99
    assert shortfall.warnings
    assert planning.bankroll_percent(1, percent="0").data["stake_centavos"] == 0
    assert planning.bankroll_percent(1, percent="100").data["stake_centavos"] == 1
    assert planning.bankroll_percent(101, stake_cents=0).data["percentage"] == 0
    assert planning.bankroll_percent(101, stake_cents=101).data["percentage"] == 100
    with pytest.raises(CalculatorInputError):
        planning.bankroll_percent(100, stake_cents=101)
    with pytest.raises(CalculatorInputError):
        planning.bankroll_percent(100, percent="100.000001")
    with pytest.raises(CalculatorInputError):
        decimal_input("1e9999999", "test")


def test_randomized_target_and_bankroll_rounding() -> None:
    rng = random.Random(105)
    for _ in range(200):
        odd = Decimal(rng.randint(110, 1000)) / 100
        target = rng.randint(1, 1_000_000)
        result = planning.target_profit(str(odd), target).data
        expected = (Decimal(target) / (odd - 1)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        assert result["stake_centavos"] == int(expected)
        assert result["realized_profit_centavos"] == int(
            (expected * (odd - 1)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        bankroll = rng.randint(1, 1_000_000)
        percent = Decimal(rng.randint(0, 10000)) / 100
        direct = planning.bankroll_percent(bankroll, percent=str(percent)).data
        expected_stake = (Decimal(bankroll) * percent / 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        assert direct["stake_centavos"] == int(expected_stake)


def test_calculator_domain_does_not_use_binary_float() -> None:
    root = Path(__file__).resolve().parents[3] / "src/bancaemdia/domain/calculators"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), path
            assert not (isinstance(node, ast.Name) and node.id == "float"), path
