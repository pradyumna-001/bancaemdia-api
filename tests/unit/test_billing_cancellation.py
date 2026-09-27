from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from bancaemdia.api.v1 import billing
from bancaemdia.config import get_settings
from bancaemdia.integrations.billing.stripe import StripeBilling


async def test_ambiguous_checkout_cannot_report_successful_cancellation(monkeypatch):
    row = SimpleNamespace(provider_customer_ref="cus_fixture", provider_subscription_ref=None)
    operation = SimpleNamespace(state="pending", session_ref=None, operation="operation")
    session = AsyncMock()
    session.get.return_value = operation
    adapter = Mock(subscriptions=AsyncMock(return_value=[]), cancel=AsyncMock())
    monkeypatch.setattr(billing, "StripeBilling", lambda settings: adapter)
    monkeypatch.setattr(billing, "locked_subscription", AsyncMock(return_value=row))
    with pytest.raises(HTTPException) as error:
        await billing.billing_cancel(SimpleNamespace(id=1), session)
    assert error.value.status_code == 409
    assert error.value.detail == "billing_reconciliation_required"
    session.commit.assert_not_called()


async def test_cancellation_recovers_subscription_before_webhook_mapping(monkeypatch):
    row = SimpleNamespace(provider_customer_ref="cus_fixture", provider_subscription_ref=None)
    operation = SimpleNamespace(state="pending", session_ref=None, operation="operation")
    session = AsyncMock()
    session.get.return_value = operation
    adapter = Mock(
        subscriptions=AsyncMock(
            return_value=[
                {
                    "id": "sub_fixture",
                    "status": "trialing",
                    "metadata": {"billing_operation": "operation"},
                }
            ]
        ),
        cancel=AsyncMock(),
    )
    monkeypatch.setattr(billing, "StripeBilling", lambda settings: adapter)
    monkeypatch.setattr(billing, "locked_subscription", AsyncMock(return_value=row))
    assert await billing.billing_cancel(SimpleNamespace(id=1), session) == {
        "status": "cancellation_scheduled"
    }
    adapter.cancel.assert_awaited_once_with("sub_fixture")
    assert row.cancel_at_period_end is True
    session.commit.assert_awaited_once()


async def test_second_cancellation_cannot_replay_a_pre_resume_response():
    calls = []

    def handle(request):
        calls.append(request)
        assert "Idempotency-Key" not in request.headers
        assert request.content == b"cancel_at_period_end=true"
        return httpx.Response(200, json={"livemode": False})

    settings = get_settings().model_copy(
        update={
            "BILLING_ENABLED": True,
            "STRIPE_SECRET_KEY": SecretStr("sk_test_fixture"),
        }
    )
    adapter = StripeBilling(settings, httpx.MockTransport(handle))
    await adapter.cancel("sub_fixture")
    await adapter.cancel("sub_fixture")
    assert len(calls) == 2
