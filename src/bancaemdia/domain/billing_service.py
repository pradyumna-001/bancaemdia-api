"""Durable provider orchestration. Redirects never modify entitlements."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.billing_catalog import BillingFrequency
from bancaemdia.integrations.billing.stripe import (
    BillingUnavailableError,
    StripeBilling,
    hosted_url,
)
from bancaemdia.models.assinatura import Assinatura
from bancaemdia.models.billing_checkout import BillingCheckout
from bancaemdia.models.billing_price import BillingPrice
from bancaemdia.repositories.billing_catalog_repo import BillingCatalogRepo


async def tenant(session: AsyncSession, uid: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(uid)}
    )


async def locked_subscription(session: AsyncSession, uid: int) -> Assinatura:
    await tenant(session, uid)
    row = await session.scalar(
        select(Assinatura)
        .where(Assinatura.usuario_id == uid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise BillingUnavailableError("billing_rollout_required")
    return row


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


async def subscribe(
    session: AsyncSession,
    uid: int,
    provider: StripeBilling,
    *,
    currency: str,
    frequency: BillingFrequency,
    request_key: str,
) -> str:
    if currency not in provider.settings.BILLING_CURRENCIES.split(","):
        raise BillingUnavailableError("billing_currency_unavailable")
    row = await locked_subscription(session, uid)
    if row.provider_customer_ref:
        await reconcile(session, uid, provider)
        if row.provider_subscription_ref and row.status not in {"CANCELED", "EXPIRED"}:
            raise BillingUnavailableError("billing_use_customer_portal")
    now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
    if row.trial_confirmed and now < row.trial_ends_at:
        raise BillingUnavailableError("billing_trial_already_granted")
    if row.provider_subscription_ref:
        remote = await provider.subscription(row.provider_subscription_ref)
        if remote.get("status") not in {"canceled", "incomplete_expired"}:
            raise BillingUnavailableError("billing_use_customer_portal")
    operation = await session.get(BillingCheckout, uid)
    if operation and operation.state == "pending":
        reserved_price = await session.get(BillingPrice, operation.price_id)
        if (
            reserved_price is None
            or reserved_price.currency != currency
            or reserved_price.frequency != frequency
        ):
            raise BillingUnavailableError("billing_checkout_terms_conflict")
    request_hash = hashlib.sha256(request_key.encode()).hexdigest()
    now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
    if operation and operation.state == "complete" and operation.request_hash == request_hash:
        raise BillingUnavailableError("billing_checkout_already_completed")
    if operation and operation.session_ref and operation.state != "complete":
        previous = await provider.request("GET", "checkout/sessions/" + operation.session_ref)
        if previous.get("status") == "open":
            return hosted_url(previous.get("url"), "checkout.stripe.com")
        if previous.get("status") == "complete":
            raise BillingUnavailableError("billing_confirmation_pending")
        if previous.get("status") != "expired":
            raise BillingUnavailableError("billing_checkout_state_unknown")
        operation.state = "expired"
    if (
        operation
        and not operation.session_ref
        and operation.state == "pending"
        and now - operation.created_at >= timedelta(hours=23)
    ):
        # Stripe may prune keys after 24h. Never turn ambiguity into another charge.
        raise BillingUnavailableError("billing_reconciliation_required")
    if operation is None or operation.state in {"complete", "expired"}:
        try:
            price = await BillingCatalogRepo().current_public_price(
                session, currency=currency, frequency=frequency
            )
        except ValueError as exc:
            raise BillingUnavailableError("billing_catalog_unpublished") from exc
        if operation is None:
            operation = BillingCheckout(usuario_id=uid)
            session.add(operation)
        operation.operation = str(uuid4())
        operation.request_hash, operation.price_id = request_hash, price.id
        operation.trial, operation.state, operation.session_ref = (
            not row.trial_confirmed,
            "pending",
            None,
        )
        operation.created_at = now
    operation_id = operation.operation
    await session.commit()  # Durable key before any external mutation.
    row = await locked_subscription(session, uid)
    operation = await session.get(BillingCheckout, uid, populate_existing=True)
    if operation is None or operation.operation != operation_id:
        raise BillingUnavailableError("billing_checkout_conflict")
    selected_price = await session.get(BillingPrice, operation.price_id)
    if selected_price is None or not selected_price.provider_plan_ref:
        raise BillingUnavailableError("billing_catalog_unpublished")
    price = selected_price
    assert price.provider_plan_ref is not None
    await provider.validate_price(
        price.provider_plan_ref,
        amount=price.amount_cents,
        currency=price.currency,
        frequency=price.frequency,
    )
    if row.provider_customer_ref is None:
        row.provider_customer_ref = await provider.customer(operation.operation)
        row.provider = "stripe"
        await session.commit()  # Persist mapping before creating Checkout.
        row = await locked_subscription(session, uid)
        operation = await session.get(BillingCheckout, uid, populate_existing=True)
        if operation is None or operation.operation != operation_id:
            raise BillingUnavailableError("billing_checkout_conflict")
    if row.provider_customer_ref is None:
        raise BillingUnavailableError("billing_customer_missing")
    result = await provider.checkout(
        row.provider_customer_ref,
        price.provider_plan_ref,
        operation.operation,
        trial=operation.trial,
    )
    operation.session_ref = result["id"]
    url = hosted_url(result.get("url"), "checkout.stripe.com")
    await session.commit()
    return url
