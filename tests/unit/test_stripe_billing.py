"""Deterministic Stripe HTTP contracts. These do not claim account/sandbox approval."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from bancaemdia.config import get_settings
from bancaemdia.domain.billing_service import project_remote
from bancaemdia.integrations.billing.stripe import (
    API_VERSION,
    BillingUnavailableError,
    StripeBilling,
    hosted_url,
    verify_event,
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


def signature(raw, stamp=1000):
    digest = hmac.new(
        b"fixture-secret", str(stamp).encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    return f"t={stamp},v1={digest}"


def test_signature_uses_raw_bytes_and_rejects_replay_and_live_events():
    raw = b'{ "id":"evt_fixture", "livemode":false }'
    assert verify_event(raw, signature(raw), "fixture-secret", now=1300)["id"] == "evt_fixture"
    for body, header, now in [
        (raw, signature(raw), 1301),
        (raw, signature(raw), 699),
        (raw.replace(b" ", b""), signature(raw), 1000),
        (raw, "t=1,t=1000,v1=x", 1000),
        (raw, "broken", 1000),
    ]:
        with pytest.raises(BillingUnavailableError):
            verify_event(body, header, "fixture-secret", now=now)
    live = json.dumps({"id": "evt_fixture", "livemode": True}).encode()
    with pytest.raises(BillingUnavailableError):
        verify_event(live, signature(live), "fixture-secret", now=1000)


@pytest.mark.parametrize(
    "value",
    [
        "http://checkout.stripe.com/x",
        "https://checkout.stripe.com.evil.test/x",
        "https://evil.test",
        "https://user@checkout.stripe.com/x",
        "https://checkout.stripe.com:443/x",
        "https://checkout.stripe.com:invalid/x",
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
    await adapter.checkout(
        "cus_fixture", "price_fixture", "operation-fixture", trial=True, currency="BRL"
    )
    await adapter.checkout(
        "cus_fixture", "price_fixture", "operation-fixture", trial=False, currency="BRL"
    )
    assert seen[0]["payment_method_collection"] == ["always"]
    assert seen[0]["currency"] == ["brl"]
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


def remote(status="active", refunded=0, disputed=False):
    return {
        "status": status,
        "current_period_start": 1000000,
        "current_period_end": 2000000,
        "items": {"data": [{"quantity": 1, "price": {"id": "price_fixture"}}]},
        "latest_invoice": {
            "status": "paid",
            "paid": True,
            "charge": {
                "paid": True,
                "amount": 100,
                "amount_refunded": refunded,
                "disputed": disputed,
            },
        },
    }


@pytest.mark.parametrize(
    "status,expected",
    [
        ("past_due", "PAST_DUE"),
        ("incomplete", "PAST_DUE"),
        ("unpaid", "PAST_DUE"),
        ("canceled", "CANCELED"),
        ("paused", "EXPIRED"),
        ("incomplete_expired", "EXPIRED"),
    ],
)
def test_remote_states_are_not_implicitly_paid(status, expected):
    assert (
        project_remote(
            remote(status),
            price_ref="price_fixture",
            trial_ends_at=datetime.fromtimestamp(900000, UTC),
        )[0]
        == expected
    )


def test_refund_dispute_and_exact_paid_period():
    cutoff = datetime.fromtimestamp(1000000, UTC)
    assert project_remote(remote(), price_ref="price_fixture", trial_ends_at=cutoff)[0] == "ACTIVE"
    assert (
        project_remote(remote(refunded=1), price_ref="price_fixture", trial_ends_at=cutoff)[0]
        == "ACTIVE"
    )
    for payload in [remote(refunded=100), remote(disputed=True)]:
        assert (
            project_remote(payload, price_ref="price_fixture", trial_ends_at=cutoff)[0]
            == "PAST_DUE"
        )
    unpaid = remote()
    unpaid["latest_invoice"]["paid"] = False
    assert project_remote(unpaid, price_ref="price_fixture", trial_ends_at=cutoff)[0] == "PAST_DUE"
    with pytest.raises(BillingUnavailableError, match="before_trial_end"):
        project_remote(
            remote(), price_ref="price_fixture", trial_ends_at=datetime.fromtimestamp(1000001, UTC)
        )
    unexpanded = remote()
    unexpanded["latest_invoice"] = "in_fixture"
    with pytest.raises(BillingUnavailableError, match="unexpanded_invoice"):
        project_remote(unexpanded, price_ref="price_fixture", trial_ends_at=cutoff)


def test_live_keys_are_rejected_before_any_request():
    from pydantic import SecretStr

    settings = get_settings().model_copy(
        update={"BILLING_ENABLED": True, "STRIPE_SECRET_KEY": SecretStr("sk_live_rejected_fixture")}
    )
    with pytest.raises(BillingUnavailableError):
        StripeBilling(settings)


def test_expired_worker_never_calls_extractor(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from bancaemdia.domain.access import AccountReadOnlyError
    from bancaemdia.workers import extraction

    monkeypatch.setattr(
        "bancaemdia.domain.access.require_worker_write_access",
        AsyncMock(side_effect=AccountReadOnlyError("account_read_only")),
    )
    reader = Mock()
    monkeypatch.setattr(extraction, "ler_mensagem", reader)
    with pytest.raises(AccountReadOnlyError):
        extraction.extrair_bilhete(123, "fixture")
    reader.assert_not_called()
