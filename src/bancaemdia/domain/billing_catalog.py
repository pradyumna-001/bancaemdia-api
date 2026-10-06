"""Validation and selection of versioned, provider-neutral prices."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

PRODUCT = "bancaemdia"


class BillingFrequency(StrEnum):
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


@dataclass(frozen=True)
class PriceTerms:
    amount_cents: int
    currency: str
    frequency: BillingFrequency
    valid_from: datetime
    valid_until: datetime | None = None
    provider_plan_ref: str | None = None
    published: bool = False

    def __post_init__(self) -> None:
        if type(self.amount_cents) is not int or self.amount_cents <= 0:
            raise ValueError("amount_cents must be a positive integer")
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("currency must be an uppercase ISO code")
        if not isinstance(self.frequency, BillingFrequency):
            raise ValueError("unsupported billing frequency")
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() is None:
            raise ValueError("valid_from must include a timezone")
        if self.valid_until is not None:
            if self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None:
                raise ValueError("valid_until must include a timezone")
            if self.valid_until <= self.valid_from:
                raise ValueError("invalid price validity")


def public_price(
    prices: list[PriceTerms],
    *,
    now: datetime,
    currency: str = "BRL",
    frequency: BillingFrequency = BillingFrequency.MONTHLY,
) -> PriceTerms:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    instant = now.astimezone(UTC)
    available = [
        price
        for price in prices
        if price.published
        and price.currency == currency
        and price.frequency == frequency
        and price.valid_from <= instant
        and (price.valid_until is None or instant < price.valid_until)
    ]
    if len(available) != 1:
        raise ValueError("checkout unavailable: no unique published price")
    return available[0]
