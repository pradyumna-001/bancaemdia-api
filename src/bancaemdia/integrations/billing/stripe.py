"""Stripe REST contract pinned independently of the account's default API version.

Only test credentials are accepted. Neither exception text nor telemetry includes
Stripe response bodies, headers, customer identifiers or hosted URL secrets.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from opentelemetry.instrumentation.utils import suppress_instrumentation

from bancaemdia.config import Settings

API_VERSION = "2024-06-20"


class _StripeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "api.stripe.com" not in record.getMessage()


logging.getLogger("httpx").addFilter(_StripeLogFilter())


class BillingUnavailableError(Exception):
    """Safe public error; provider content must never be included."""


def hosted_url(value: object, host: str) -> str:
    if not isinstance(value, str):
        raise BillingUnavailableError("billing_invalid_url")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.username or parsed.port:
        raise BillingUnavailableError("billing_invalid_url")
    return value


def verify_event(
    raw: bytes, signature: str, secret: str, *, now: int | None = None
) -> dict[str, Any]:
    """Authenticate exact bytes, enforce replay tolerance, then parse minimal envelope."""
    try:
        fields = [part.split("=", 1) for part in signature.split(",")]
        timestamps = [int(v) for k, v in fields if k == "t"]
        signatures = [v for k, v in fields if k == "v1"]
        if (
            len(timestamps) != 1
            or abs((int(time.time()) if now is None else now) - timestamps[0]) > 300
        ):
            raise ValueError
        expected = hmac.new(
            secret.encode(), str(timestamps[0]).encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected, value) for value in signatures):
            raise ValueError
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("livemode") is not False:
            raise ValueError
        if not isinstance(event.get("id"), str) or not event["id"].startswith("evt_"):
            raise ValueError
        return event
    except (ValueError, TypeError, KeyError) as exc:
        raise BillingUnavailableError("billing_invalid_signature") from exc


class StripeBilling:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        key = settings.STRIPE_SECRET_KEY
        if (
            not settings.BILLING_ENABLED
            or key is None
            or not key.get_secret_value().startswith(("sk_test_", "rk_test_"))
        ):
            raise BillingUnavailableError("billing_test_configuration_required")
        self.key = key.get_secret_value()
        self.settings = settings
        self.transport = transport

    async def request(
        self, method: str, path: str, data: dict[str, str] | None = None, *, key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.key}", "Stripe-Version": API_VERSION}
        if key:
            headers["Idempotency-Key"] = key
        try:
            # Stripe URL paths contain identifiers. Suppress automatic HTTP tracing.
            with suppress_instrumentation():
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=20, follow_redirects=False
                ) as client:
                    response = await client.request(
                        method,
                        "https://api.stripe.com/v1/" + path,
                        headers=headers,
                        params=data if method == "GET" else None,
                        data=data if method != "GET" else None,
                    )
            if response.status_code >= 400:
                raise BillingUnavailableError("billing_provider_unavailable")
            result: dict[str, Any] = response.json()
            if result.get("livemode") is True:
                raise BillingUnavailableError("billing_live_object_rejected")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise BillingUnavailableError("billing_provider_unavailable") from exc

    async def customer(self, operation: str) -> str:
        result = await self.request(
            "POST",
            "customers",
            {"metadata[billing_operation]": operation},
            key="customer-" + operation,
        )
        return str(result["id"])

    async def validate_price(self, ref: str, *, amount: int, currency: str, frequency: str) -> None:
        price = await self.request("GET", "prices/" + ref)
        recurring = price.get("recurring") or {}
        if (
            price.get("livemode") is not False
            or not price.get("active")
            or price.get("unit_amount") != amount
            or price.get("currency") != currency.lower()
            or recurring.get("interval") != {"MONTHLY": "month", "YEARLY": "year"}[frequency]
            or recurring.get("interval_count") != 1
            or recurring.get("usage_type") != "licensed"
            or recurring.get("trial_period_days")
            or price.get("billing_scheme") != "per_unit"
            or price.get("transform_quantity")
        ):
            raise BillingUnavailableError("billing_price_mismatch")

    async def checkout(
        self, customer: str, price: str, operation: str, *, trial: bool
    ) -> dict[str, Any]:
        data = {
            "mode": "subscription",
            "customer": customer,
            "line_items[0][price]": price,
            "line_items[0][quantity]": "1",
            "payment_method_collection": "always",
            "payment_method_types[0]": "card",
            "success_url": self.settings.BILLING_RETURN_URL,
            "cancel_url": self.settings.BILLING_RETURN_URL,
            "subscription_data[metadata][billing_operation]": operation,
            "metadata[billing_operation]": operation,
            "allow_promotion_codes": "false",
            "automatic_tax[enabled]": "true",
        }
        if trial:
            data["subscription_data[trial_period_days]"] = "7"
            data["subscription_data[trial_settings][end_behavior][missing_payment_method]"] = (
                "cancel"
            )
        data["customer_update[address]"] = "auto"
        data["billing_address_collection"] = "required"
        return await self.request("POST", "checkout/sessions", data, key="checkout-" + operation)

    async def subscriptions(self, customer: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        params = {"customer": customer, "status": "all", "limit": "100"}
        while True:
            page = await self.request("GET", "subscriptions", params)
            rows.extend(page["data"])
            if not page.get("has_more"):
                return rows
            params["starting_after"] = page["data"][-1]["id"]

    async def subscription(self, ref: str) -> dict[str, Any]:
        return await self.request(
            "GET",
            "subscriptions/" + ref,
            {"expand[0]": "latest_invoice.charge", "expand[1]": "default_payment_method"},
        )

    async def cancel(self, ref: str) -> None:
        await self.request(
            "POST", "subscriptions/" + ref, {"cancel_at_period_end": "true"}, key="cancel-" + ref
        )

    async def portal(self, customer: str) -> str:
        config = self.settings.STRIPE_PORTAL_CONFIGURATION
        if not config:
            raise BillingUnavailableError("billing_portal_configuration_required")
        configuration = await self.request("GET", "billing_portal/configurations/" + config)
        features = configuration.get("features", {})
        cancel = features.get("subscription_cancel", {})
        if (
            not configuration.get("active")
            or features.get("subscription_update", {}).get("enabled")
            or not cancel.get("enabled")
            or cancel.get("mode") != "at_period_end"
        ):
            raise BillingUnavailableError("billing_unsafe_portal_configuration")
        result = await self.request(
            "POST",
            "billing_portal/sessions",
            {
                "customer": customer,
                "configuration": config,
                "return_url": self.settings.BILLING_RETURN_URL,
            },
        )
        return hosted_url(result.get("url"), "billing.stripe.com")
