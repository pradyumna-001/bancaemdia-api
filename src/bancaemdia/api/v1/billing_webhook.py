from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import ErrorResponse
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db_primary
from bancaemdia.integrations.billing.stripe import BillingUnavailableError, verify_event
from bancaemdia.models.billing_event import BillingEvent

router = APIRouter(
    responses={
        400: {"model": ErrorResponse, "description": "Invalid signature or event."},
        413: {"model": ErrorResponse, "description": "Event too large."},
        503: {"model": ErrorResponse, "description": "Webhook temporarily unavailable."},
    }
)
EVENTS = frozenset({
    "checkout.session.completed",
    "checkout.session.expired",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_action_required",
    "invoice.voided",
    "invoice.marked_uncollectible",
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.closed",
})


@router.post(
    "/api/v1/billing/webhook",
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "Raw Stripe JSON bytes; signature verification precedes parsing.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/components/schemas/JsonValue"},
                    },
                    "example": {
                        "id": "evt_contract",
                        "type": "invoice.paid",
                        "livemode": False,
                        "data": {"object": {"customer": "cus_contract"}},
                    },
                }
            },
        },
        "security": [{"StripeSignature": []}],
    },
)
async def stripe_webhook(
    request: Request, session: AsyncSession = Depends(get_db_primary)
) -> dict[str, str]:
    settings = get_settings()
    if not settings.BILLING_ENABLED or settings.STRIPE_WEBHOOK_SECRET is None:
        raise HTTPException(503, "billing_webhook_unavailable")
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > 262144:
            raise HTTPException(413, "billing_event_too_large")
    try:
        event = verify_event(
            bytes(raw),
            request.headers.get("stripe-signature", ""),
            settings.STRIPE_WEBHOOK_SECRET.get_secret_value(),
        )
    except BillingUnavailableError:
        raise HTTPException(400, "billing_invalid_signature") from None
    if event.get("type") not in EVENTS:
        return {"status": "ignored"}
    data = event.get("data")
    resource = data.get("object") if isinstance(data, dict) else None
    if not isinstance(resource, dict):
        raise HTTPException(400, "billing_invalid_event")
    customer = resource.get("customer")
    # Disputes lack a customer; the periodic full reconciliation still repairs them.
    # Resolve their charge through Stripe rather than storing the raw dispute payload.
    if not customer and event["type"].startswith("charge.dispute."):
        from bancaemdia.integrations.billing.stripe import StripeBilling

        try:
            charge = await StripeBilling(settings).request("GET", "charges/" + resource["charge"])
            customer = charge.get("customer")
        except (BillingUnavailableError, KeyError, TypeError):
            raise HTTPException(503, "billing_provider_unavailable") from None
    if not isinstance(customer, str) or not customer.startswith("cus_"):
        raise HTTPException(400, "billing_customer_missing")
    await session.execute(
        insert(BillingEvent)
        .values(id=event["id"], customer_ref=customer, event_type=event["type"])
        .on_conflict_do_nothing(index_elements=[BillingEvent.id])
    )
    await session.commit()
    # A DB-backed periodic worker consumes this inbox: no commit/enqueue crash gap.
    return {"status": "accepted"}
