from datetime import UTC, datetime, timedelta

import pytest

from bancaemdia.domain.billing import (
    AccessMode,
    BillingSnapshot,
    SubscriptionStatus,
    access_mode,
    read_model,
    trial_bounds,
)
from bancaemdia.domain.billing_catalog import BillingFrequency, PriceTerms, public_price

START = datetime(2026, 9, 23, 12, tzinfo=UTC)
END = START + timedelta(days=7)


def trial(status: SubscriptionStatus = SubscriptionStatus.TRIALING) -> BillingSnapshot:
    return BillingSnapshot(status=status, trial_started_at=START, trial_ends_at=END)


def test_trial_is_seven_complete_days_with_half_open_end() -> None:
    assert trial_bounds(START) == (START, END)
    assert access_mode(trial(), now=END - timedelta(microseconds=1), rollout_at=START) == (
        AccessMode.FULL_WRITE
    )
    assert access_mode(trial(), now=END, rollout_at=START) == AccessMode.READ_ONLY
    assert read_model(trial(), now=END, rollout_at=START).status == SubscriptionStatus.EXPIRED


def test_rollout_off_is_safe_and_missing_row_after_rollout_is_closed() -> None:
    assert access_mode(None, now=END, rollout_at=None) == AccessMode.FULL_WRITE
    assert access_mode(None, now=END, rollout_at=START) == AccessMode.READ_ONLY


def test_trial_is_not_conditioned_on_provider_price_or_subscription_state() -> None:
    for status in SubscriptionStatus:
        assert access_mode(trial(status), now=START, rollout_at=START) == AccessMode.FULL_WRITE
    active = BillingSnapshot(
        SubscriptionStatus.ACTIVE, START, END, END, END + timedelta(days=30), 42
    )
    assert access_mode(active, now=END, rollout_at=START) == AccessMode.FULL_WRITE
    assert access_mode(active, now=END + timedelta(days=30), rollout_at=START) == (
        AccessMode.READ_ONLY
    )


def test_billing_rejects_naive_time_and_invalid_trial_bounds() -> None:
    with pytest.raises(ValueError):
        trial_bounds(datetime(2026, 9, 23))
    with pytest.raises(ValueError):
        access_mode(trial(), now=datetime(2026, 9, 23), rollout_at=START)
    invalid = BillingSnapshot(SubscriptionStatus.TRIALING, START, END + timedelta(seconds=1))
    with pytest.raises(ValueError):
        access_mode(invalid, now=START, rollout_at=START)


def test_unpublished_catalog_refuses_checkout() -> None:
    price = PriceTerms(100, "BRL", BillingFrequency.MONTHLY, START)
    with pytest.raises(ValueError, match="checkout unavailable"):
        public_price([price], now=START)
    assert (
        public_price(
            [PriceTerms(100, "BRL", BillingFrequency.MONTHLY, START, published=True)], now=START
        ).amount_cents
        == 100
    )


@pytest.mark.parametrize("amount", [0, -1, 1.0, True])
def test_invalid_centavos_are_refused(amount: object) -> None:
    with pytest.raises(ValueError):
        PriceTerms(amount, "BRL", BillingFrequency.MONTHLY, START)  # type: ignore[arg-type]


def test_invalid_currency_frequency_and_validity_are_refused() -> None:
    with pytest.raises(ValueError):
        PriceTerms(100, "USD", BillingFrequency.MONTHLY, START)
    with pytest.raises(ValueError):
        PriceTerms(100, "BRL", "WEEKLY", START)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PriceTerms(100, "BRL", BillingFrequency.MONTHLY, START, START)
