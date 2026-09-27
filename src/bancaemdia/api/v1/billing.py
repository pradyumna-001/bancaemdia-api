from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse
from bancaemdia.api.deps import get_current_user
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db_primary
from bancaemdia.domain.billing_catalog import BillingFrequency
from bancaemdia.domain.billing_checkout import locked_subscription, subscribe
from bancaemdia.domain.registros import Usuario
from bancaemdia.integrations.billing.stripe import BillingUnavailableError, StripeBilling
from bancaemdia.models.assinatura import Assinatura
from bancaemdia.models.billing_price import BillingPrice
from bancaemdia.repositories.assinatura_repo import AssinaturaRepo

router = APIRouter(
    prefix="/api/v1/billing",
    tags=["billing"],
    responses={
        **AUTHENTICATED_ERROR_RESPONSES,
        409: {
            "model": ErrorResponse,
            "description": "Billing configuration or current subscription prevents this action.",
        },
    },
)
User = Annotated[Usuario, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_db_primary)]


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(default="BRL", pattern="^[A-Z]{3}$")
    frequency: BillingFrequency = BillingFrequency.MONTHLY


class PublicPrice(BaseModel):
    id: int
    amount_minor: int
    currency: str
    frequency: BillingFrequency


class BillingStatusResponse(BaseModel):
    status: Literal["AWAITING_CARD", "TRIALING", "ACTIVE", "PAST_DUE", "CANCELED", "EXPIRED"] | None
    access: Literal["FULL_WRITE", "READ_ONLY"]
    trial_started_at: datetime | None
    trial_ends_at: datetime | None
    current_period_ends_at: datetime | None
    price_id: int | None
    trial_confirmed: bool
    cancel_at_period_end: bool
    card_required: bool
    prices: list[PublicPrice]
    checkout_available: bool
    can_manage: bool


class HostedResponse(BaseModel):
    url: str


@router.get("/status", response_model=BillingStatusResponse)
async def billing_status(user: User, session: Session, response: Response) -> BillingStatusResponse:
    response.headers["Cache-Control"] = "no-store"
    read = await AssinaturaRepo().read_status(session, user.id)
    row = await session.get(Assinatura, user.id)
    now = await session.scalar(select(func.clock_timestamp()))
    prices = (
        await session.scalars(
            select(BillingPrice).where(
                BillingPrice.published.is_(True),
                BillingPrice.valid_from <= now,
                (BillingPrice.valid_until.is_(None) | (BillingPrice.valid_until > now)),
            )
        )
    ).all()
    confirmed = bool(row and row.trial_confirmed)
    result = asdict(read)
    if not confirmed:
        result.update(trial_started_at=None, trial_ends_at=None, status="AWAITING_CARD")
    result.update(
        trial_confirmed=confirmed,
        cancel_at_period_end=bool(row and row.cancel_at_period_end),
        card_required=True,
        can_manage=bool(row and row.provider_customer_ref),
        prices=[
            {
                "id": p.id,
                "amount_minor": p.amount_cents,
                "currency": p.currency,
                "frequency": p.frequency,
            }
            for p in prices
            if p.currency in get_settings().BILLING_CURRENCIES.split(",")
        ],
        checkout_available=get_settings().BILLING_ENABLED and bool(prices),
    )
    return BillingStatusResponse.model_validate(result)


@router.post("/subscribe", response_model=HostedResponse)
async def billing_subscribe(
    body: SubscribeRequest,
    user: User,
    session: Session,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> HostedResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        url = await subscribe(
            session,
            user.id,
            StripeBilling(get_settings()),
            currency=body.currency,
            frequency=body.frequency,
            request_key=idempotency_key,
        )
        return HostedResponse(url=url)
    except BillingUnavailableError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/portal", response_model=HostedResponse)
async def billing_portal(user: User, session: Session, response: Response) -> HostedResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        row = await locked_subscription(session, user.id)
        if not row.provider_customer_ref:
            raise BillingUnavailableError("billing_customer_missing")
        return HostedResponse(
            url=await StripeBilling(get_settings()).portal(row.provider_customer_ref)
        )
    except BillingUnavailableError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/cancel")
async def billing_cancel(user: User, session: Session) -> dict[str, str]:
    try:
        provider = StripeBilling(get_settings())
        row = await locked_subscription(session, user.id)
        if row.provider_subscription_ref and row.status != "CANCELED":
            await provider.cancel(row.provider_subscription_ref)
            await session.commit()
        return {"status": "cancellation_scheduled"}
    except BillingUnavailableError as exc:
        raise HTTPException(409, str(exc)) from None
