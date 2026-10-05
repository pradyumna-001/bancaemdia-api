"""The HTTP boundary never parses business data or writes before authentication."""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from bancaemdia.api.v1 import billing_webhook
from bancaemdia.config import get_settings


def incoming(body, *, valid=True):
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(b"fixture", stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    header = f"t={stamp},v1={digest if valid else 'forged'}"
    return Request(
        {"type": "http", "headers": [(b"stripe-signature", header.encode())]},
        AsyncMock(return_value={"type": "http.request", "body": raw, "more_body": False}),
    )


@pytest.fixture
def configured(monkeypatch):
    settings = get_settings().model_copy(
        update={"BILLING_ENABLED": True, "STRIPE_WEBHOOK_SECRET": SecretStr("fixture")}
    )
    monkeypatch.setattr(billing_webhook, "get_settings", lambda: settings)


async def test_forged_webhook_never_writes(configured):
    session = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await billing_webhook.stripe_webhook(incoming({}, valid=False), session)
    assert exc.value.status_code == 400
    session.execute.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("data", [None, [], {"object": []}, {"object": None}])
async def test_signed_malformed_resource_is_rejected(configured, data):
    session = AsyncMock()
    event = {"id": "evt_fixture", "type": "invoice.paid", "livemode": False, "data": data}
    with pytest.raises(HTTPException) as exc:
        await billing_webhook.stripe_webhook(incoming(event), session)
    assert exc.value.status_code == 400
    session.execute.assert_not_called()


async def test_verified_event_is_committed_before_ack(configured):
    session = AsyncMock()
    event = {
        "id": "evt_fixture",
        "type": "invoice.paid",
        "livemode": False,
        "data": {"object": {"customer": "cus_fixture", "email": "discard@test.invalid"}},
    }
    assert await billing_webhook.stripe_webhook(incoming(event), session) == {"status": "accepted"}
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
    statement = session.execute.call_args.args[0]
    values = statement.compile().params
    assert "discard@test.invalid" not in str(values)
    assert values["id"] == "evt_fixture"
