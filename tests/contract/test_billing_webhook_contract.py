"""Exercise the signed provider route through the actual HTTP middleware."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from bancaemdia.api.v1 import billing_webhook
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db_primary
from bancaemdia.main import app


@pytest.mark.parametrize(
    ("enabled", "body", "status"),
    [(True, b"{}", 400), (True, b"x" * 262145, 413), (False, b"{}", 503)],
    ids=["missing-signature", "oversized-body", "billing-disabled"],
)
def test_webhook_failure_contract_rejects_before_writes(monkeypatch, enabled, body, status):
    configured = get_settings().model_copy(
        update={"BILLING_ENABLED": enabled, "STRIPE_WEBHOOK_SECRET": SecretStr("fixture")}
    )
    monkeypatch.setattr(billing_webhook, "get_settings", lambda: configured)
    session = AsyncMock()
    monkeypatch.setitem(app.dependency_overrides, get_db_primary, lambda: session)
    response = TestClient(app).post(
        "/api/v1/billing/webhook", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == status
    assert set(response.json()) == {"detail"}
    session.execute.assert_not_called()
    session.commit.assert_not_called()
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/billing/webhook"]["post"]
    assert operation["security"] == [{"StripeSignature": []}]
    assert str(status) in operation["responses"]
    assert "application/json" in operation["requestBody"]["content"]
