"""Durable provider orchestration. Redirects never modify entitlements."""

import hashlib
from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.billing_catalog import BillingFrequency
from bancaemdia.integrations.billing.base import BillingProvider
from bancaemdia.integrations.billing.stripe import (
    BillingUnavailableError,
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


async def subscribe(
    session: AsyncSession,
    uid: int,
    provider: BillingProvider,
    *,
    currency: str,
    frequency: BillingFrequency,
    request_key: str,
) -> str:
    if currency not in provider.settings.BILLING_CURRENCIES.split(","):
        raise BillingUnavailableError("billing_currency_unavailable")
    row = await locked_subscription(session, uid)
    if row.provider_customer_ref:
        existing = await provider.subscriptions(row.provider_customer_ref)
        if any(r.get("status") not in {"canceled", "incomplete_expired"} for r in existing):
            raise BillingUnavailableError("billing_use_customer_portal")
        if not row.trial_confirmed and any(r.get("trial_start") for r in existing):
            raise BillingUnavailableError("billing_confirmation_pending")
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
    if row.trial_confirmed and now < row.trial_ends_at:
        raise BillingUnavailableError("billing_trial_already_granted")
    if operation and operation.state == "complete" and operation.request_hash == request_hash:
        raise BillingUnavailableError("billing_checkout_already_completed")
    if operation and operation.session_ref and operation.state != "complete":
        previous = await provider.checkout_status(operation.session_ref)
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
        currency=price.currency,
    )
    operation.session_ref = result["id"]
    url = hosted_url(result.get("url"), "checkout.stripe.com")
    await session.commit()
    return url
