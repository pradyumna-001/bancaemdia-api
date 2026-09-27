"""Deterministic Stripe HTTP contracts. These do not claim account/sandbox approval."""

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from bancaemdia.domain.billing_service import project_remote
from bancaemdia.integrations.billing.stripe import (
    BillingUnavailableError,
    verify_event,
)


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
