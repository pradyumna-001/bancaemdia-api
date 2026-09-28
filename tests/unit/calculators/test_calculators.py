"""Independent vectors and bounded invariants for the selected calculators."""

import ast
import random
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from bancaemdia.domain.calculators import allocation as alloc
from bancaemdia.domain.calculators import planning, probability
from bancaemdia.domain.calculators.core import CalculatorInputError, allocation, decimal_input


def test_fair_market_vectors() -> None:
    fair = probability.fair([("A", "2.0"), ("B", "2.00")]).data
    assert [leg["fair_probability"] for leg in fair["outcomes"]] == [
        Decimal("0.50000000"),
        Decimal("0.50000000"),
    ]
    assert fair["outcomes"][0]["implied_probability"] == Decimal("0.50000000")
    assert fair["overround"] == Decimal("0E-8")
    three = probability.fair([("A", "3"), ("B", "3"), ("C", "3")]).data
    assert sum(leg["fair_probability"] for leg in three["outcomes"]) == 1
    assert three["outcomes"][0]["fair_probability"] == Decimal("0.33333334")


@pytest.mark.parametrize(
    "bad", ["1", "0", "NaN", "Infinity", "1e100000", "2.000000000", "9999999999999", "-2", "abc"]
)
def test_bad_odds_rejected(bad: str) -> None:
    with pytest.raises(CalculatorInputError):
        probability.fair([("A", bad), ("B", "2")])


def test_market_structure_and_order() -> None:
    for values in [[("A", "2")], [("A", "2"), ("A", "3")], [("A", "2"), ("a", "3")]]:
        with pytest.raises(CalculatorInputError):
            probability.fair(values)
    with pytest.raises(CalculatorInputError):
        probability.fair([("A ", "2"), ("B", "3")])
    original = [("A", "1.8"), ("B", "3.2"), ("C", "5")]
    assert [leg["name"] for leg in probability.fair(list(reversed(original))).data["outcomes"]] == [
        "C",
        "B",
        "A",
    ]
    assert probability.fair([("A", "1000000"), ("B", "2")]).data["outcomes"][0][
        "implied_probability"
    ] == Decimal("0.00000100")


def test_randomized_fair_probabilities_reconcile() -> None:
    rng = random.Random(103)
    for _ in range(200):
        prices = [
            (str(i), str(Decimal(rng.randint(101, 100_000)) / 100))
            for i in range(rng.randint(2, 20))
        ]
        first = probability.fair(prices).data
        assert first == probability.fair(prices).data
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


def test_distribution_reports_profitable_and_losing_scenarios() -> None:
    bets = [("A", "2.10"), ("B", "2.10")]
    result = alloc.distribute(bets, 10_000).data
    assert [leg["stake_centavos"] for leg in result["scenarios"]] == [5_000, 5_000]
    assert [leg["profit_centavos"] for leg in result["scenarios"]] == [500, 500]
    assert result["minimum_profit_centavos"] == 500
    assert result["guaranteed_profit"] is True
    tiny = alloc.distribute(bets, 1).data
    assert tiny["theoretical_arbitrage"] is True
    assert tiny["guaranteed_profit"] is False
    losing = alloc.distribute([("A", "1.9"), ("B", "1.9")], 10_000)
    assert losing.data["minimum_profit_centavos"] == -500
    assert losing.data["guaranteed_profit"] is False
    assert losing.warnings
    for total in range(1, 250):
        checked = alloc.distribute([("A", "2.05"), ("B", "2.10"), ("C", "7")], total).data
        profits = [leg["profit_centavos"] for leg in checked["scenarios"]]
        assert checked["minimum_profit_centavos"] == min(profits)
        assert checked["guaranteed_profit"] is False or all(profit > 0 for profit in profits)
        assert sum(leg["stake_centavos"] for leg in checked["scenarios"]) == total


def test_hedge_vectors_commission_and_residuals() -> None:
    result = planning.live_hedge(10_000, "1.5", "3").data
    assert result["hedge_stake_centavos"] == 5_000
    assert result["original_wins"]["profit_centavos"] == 0
    assert result["hedge_wins"]["profit_centavos"] == 0
    commissioned = planning.live_hedge(10_000, "1.5", "2", "10").data
    hedge = commissioned["hedge_stake_centavos"]
    expected_return = (Decimal(hedge) * Decimal("1.9")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    assert commissioned["hedge_wins"]["return_centavos"] == int(expected_return)
    assert commissioned["hedge_wins"]["profit_centavos"] == int(expected_return) - 10_000 - hedge
    assert commissioned["original_wins"]["profit_centavos"] < 0
    assert commissioned["both_outcomes_protected"] is False


def test_randomized_hedge_scenarios_recompute_from_reported_inputs() -> None:
    rng = random.Random(104)
    for _ in range(200):
        original_stake = rng.randint(1, 1_000_000)
        first = Decimal(rng.randint(101, 500)) / 100
        second = Decimal(rng.randint(101, 500)) / 100
        commission = Decimal(rng.randint(0, 50))
        data = planning.live_hedge(original_stake, str(first), str(second), str(commission)).data
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


def test_bankroll_boundaries_and_rounding() -> None:
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
    rng = random.Random(105)
    for _ in range(200):
        bankroll = rng.randint(1, 1_000_000)
        percent = Decimal(rng.randint(0, 10000)) / 100
        direct = planning.bankroll_percent(bankroll, percent=str(percent)).data
        expected = (Decimal(bankroll) * percent / 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        assert direct["stake_centavos"] == int(expected)


def test_calculator_domain_does_not_use_binary_float() -> None:
    root = Path(__file__).resolve().parents[3] / "src/bancaemdia/domain/calculators"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), path
            assert not (isinstance(node, ast.Name) and node.id == "float"), path
