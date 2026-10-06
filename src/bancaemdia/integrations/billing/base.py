"""Billing boundary; transport and credentials remain inside the selected adapter."""

from typing import Any, Protocol

from bancaemdia.config import Settings


class BillingUnavailableError(Exception):
    """Safe public error; provider content must never be included."""


class BillingProvider(Protocol):
    settings: Settings

    async def customer(self, operation: str) -> str: ...

    async def validate_price(
        self, ref: str, *, amount: int, currency: str, frequency: str
    ) -> None: ...

    async def checkout(
        self, customer: str, price: str, operation: str, *, trial: bool, currency: str
    ) -> dict[str, Any]: ...

    async def subscriptions(self, customer: str) -> list[dict[str, Any]]: ...

    async def subscription(self, ref: str) -> dict[str, Any]: ...

    async def checkout_status(self, ref: str) -> dict[str, Any]: ...

    async def cancel(self, ref: str) -> None: ...

    async def portal(self, customer: str) -> str: ...
