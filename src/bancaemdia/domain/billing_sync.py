"""Durable provider orchestration. Redirects never modify entitlements."""

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.billing_checkout import locked_subscription
from bancaemdia.integrations.billing.stripe import (
    BillingUnavailableError,
    StripeBilling,
)
from bancaemdia.models.billing_checkout import BillingCheckout
from bancaemdia.models.billing_price import BillingPrice


def project_remote(
    remote: dict[str, Any], *, price_ref: str, trial_ends_at: datetime
) -> tuple[str, datetime | None, datetime | None]:
    """A paid, unrefunded current invoice is required; active status alone isn't enough."""
    status = remote.get("status")
    if status == "canceled":
        return "CANCELED", None, None
    if status in {"past_due", "unpaid", "incomplete"}:
        return "PAST_DUE", None, None
    if status != "active" or remote.get("pause_collection"):
        return "EXPIRED", None, None
    items = remote.get("items", {}).get("data", [])
    if (
        len(items) != 1
        or items[0].get("price", {}).get("id") != price_ref
        or items[0].get("quantity") != 1
    ):
        raise BillingUnavailableError("billing_subscription_terms_mismatch")
    invoice = remote.get("latest_invoice") or {}
    charge = invoice.get("charge") or {}
    if not isinstance(invoice, dict) or not isinstance(charge, dict):
        raise BillingUnavailableError("billing_unexpanded_invoice")
    # Partial refunds retain access; full refunds or disputes revoke this period.
    if (
        invoice.get("status") != "paid"
        or invoice.get("paid") is not True
        or not charge.get("paid")
        or charge.get("disputed")
        or charge.get("amount_refunded", 0) >= charge.get("amount", 0)
    ):
        return "PAST_DUE", None, None
    start = datetime.fromtimestamp(remote["current_period_start"], UTC)
    end = datetime.fromtimestamp(remote["current_period_end"], UTC)
    if start < trial_ends_at or end <= start:
        raise BillingUnavailableError("billing_period_before_trial_end")
    return "ACTIVE", start, end


async def reconcile(session: AsyncSession, uid: int, provider: StripeBilling) -> None:
    row = await locked_subscription(session, uid)
    if not row.provider_customer_ref:
        return
    operation = await session.get(BillingCheckout, uid)
    remotes = await provider.subscriptions(row.provider_customer_ref)
    # Only subscriptions created by our durable operation or previously bound ref
    # are eligible. Unknown/manual subscriptions are not silently adopted.
    known = [
        r
        for r in remotes
        if r["id"] == row.provider_subscription_ref
        or (operation and r.get("metadata", {}).get("billing_operation") == operation.operation)
    ]
    live = [r for r in known if r.get("status") not in {"canceled", "incomplete_expired"}]
    if len(live) > 1:
        raise BillingUnavailableError("billing_duplicate_remote_subscription")
    chosen = (
        live[0]
        if live
        else next((r for r in known if r["id"] == row.provider_subscription_ref), None)
    )
    if chosen is None:
        row.last_reconciled_at = cast(
            datetime, await session.scalar(select(func.clock_timestamp()))
        )
        return
    remote = await provider.subscription(chosen["id"])
    if remote.get("customer") != row.provider_customer_ref or remote.get("livemode") is not False:
        raise BillingUnavailableError("billing_customer_mismatch")
    price_id = (
        operation.price_id
        if operation and remote.get("metadata", {}).get("billing_operation") == operation.operation
        else row.price_id
    )
    price = await session.get(BillingPrice, price_id) if price_id else None
    if price is None or not price.provider_plan_ref:
        raise BillingUnavailableError("billing_price_missing")
    items = remote.get("items", {}).get("data", [])
    if len(items) != 1 or items[0].get("price", {}).get("id") != price.provider_plan_ref:
        raise BillingUnavailableError("billing_subscription_terms_mismatch")
    if not row.trial_confirmed:
        method = remote.get("default_payment_method")
        if (
            not isinstance(method, dict)
            or method.get("type") != "card"
            or method.get("customer") != row.provider_customer_ref
        ):
            raise BillingUnavailableError("billing_card_confirmation_pending")
        start, end = remote.get("trial_start"), remote.get("trial_end")
        if not isinstance(start, int) or not isinstance(end, int) or end - start != 7 * 86400:
            raise BillingUnavailableError("billing_invalid_trial")
        row.trial_started_at = datetime.fromtimestamp(start, UTC)
        row.trial_ends_at = datetime.fromtimestamp(end, UTC)
        row.trial_confirmed = True
    if (
        remote.get("trial_end")
        and datetime.fromtimestamp(remote["trial_end"], UTC) != row.trial_ends_at
    ):
        raise BillingUnavailableError("billing_trial_changed")
    row.provider, row.provider_subscription_ref, row.price_id = "stripe", remote["id"], price.id
    now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
    if remote.get("status") == "trialing" and now < row.trial_ends_at:
        row.status = "TRIALING"
        row.current_period_started_at = row.current_period_ends_at = None
    else:
        row.status, row.current_period_started_at, row.current_period_ends_at = project_remote(
            remote, price_ref=price.provider_plan_ref, trial_ends_at=row.trial_ends_at
        )
    row.cancel_at_period_end = bool(remote.get("cancel_at_period_end"))
    row.last_reconciled_at = now
    if operation:
        operation.state = "complete"
    await session.flush()
