"""Deterministic Stripe HTTP contracts. These do not claim account/sandbox approval."""

from urllib.parse import parse_qs

import httpx
import pytest

from bancaemdia.config import get_settings
from bancaemdia.integrations.billing.stripe import (
    API_VERSION,
    BillingUnavailableError,
    StripeBilling,
    hosted_url,
)


def provider(handler):
    from pydantic import SecretStr

    settings = get_settings().model_copy(
        update={
            "BILLING_ENABLED": True,
            "STRIPE_SECRET_KEY": SecretStr("sk_test_contract_only"),
            "STRIPE_PORTAL_CONFIGURATION": "bpc_fixture",
        }
    )
    return StripeBilling(settings, httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "value",
    [
        "http://checkout.stripe.com/x",
        "https://checkout.stripe.com.evil.test/x",
        "https://evil.test",
        "https://user@checkout.stripe.com/x",
        "https://checkout.stripe.com:443/x",
        None,
    ],
)
def test_hosted_urls_cannot_redirect_to_another_origin(value):
    with pytest.raises(BillingUnavailableError):
        hosted_url(value, "checkout.stripe.com")


async def test_checkout_always_collects_card_and_first_trial_is_seven_days():
    seen = []

    def handle(request):
        assert request.headers["Stripe-Version"] == API_VERSION
        seen.append(parse_qs(request.content.decode()))
        assert request.headers["Idempotency-Key"] == "checkout-operation-fixture"
        return httpx.Response(
            200,
            json={
                "id": "cs_test_fixture",
                "livemode": False,
                "url": "https://checkout.stripe.com/c/pay/fixture",
            },
        )

    adapter = provider(handle)
    await adapter.checkout("cus_fixture", "price_fixture", "operation-fixture", trial=True)
    await adapter.checkout("cus_fixture", "price_fixture", "operation-fixture", trial=False)
    assert seen[0]["payment_method_collection"] == ["always"]
    assert seen[0]["subscription_data[trial_period_days]"] == ["7"]
    assert seen[0]["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == [
        "cancel"
    ]
    assert "subscription_data[trial_period_days]" not in seen[1]
    assert seen[0]["line_items[0][price]"] == ["price_fixture"]
    assert "price_data" not in str(seen)


async def test_price_mismatch_and_unsafe_portal_are_rejected():
    adapter = provider(
        lambda request: httpx.Response(
            200, json={"active": True, "features": {"subscription_update": {"enabled": True}}}
        )
    )
    with pytest.raises(BillingUnavailableError, match="price_mismatch"):
        await adapter.validate_price(
            "price_fixture", amount=100, currency="JPY", frequency="MONTHLY"
        )
    with pytest.raises(BillingUnavailableError, match="unsafe_portal"):
        await adapter.portal("cus_fixture")


async def test_provider_error_never_exposes_body_or_credentials():
    adapter = provider(
        lambda request: httpx.Response(402, json={"error": {"message": "private fixture value"}})
    )
    with pytest.raises(BillingUnavailableError) as error:
        await adapter.customer("operation-fixture")
    assert str(error.value) == "billing_provider_unavailable"


def test_live_keys_are_rejected_before_any_request():
    from pydantic import SecretStr

    settings = get_settings().model_copy(
        update={"BILLING_ENABLED": True, "STRIPE_SECRET_KEY": SecretStr("sk_live_rejected_fixture")}
    )
    with pytest.raises(BillingUnavailableError):
        StripeBilling(settings)
