"""Billing persistence; write operations use the primary transaction."""

from datetime import datetime
from typing import cast

from sqlalchemy import bindparam, func, lambda_stmt, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.billing import (
    BillingReadModel,
    BillingSnapshot,
    SubscriptionStatus,
    read_model,
)
from bancaemdia.models.assinatura import Assinatura
from bancaemdia.models.billing_rollout import BillingRollout

_subscription_by_owner = lambda_stmt(
    lambda: select(Assinatura).where(Assinatura.usuario_id == bindparam("billing_owner"))
)
_live_billing_status = lambda_stmt(
    lambda: (
        select(func.clock_timestamp(), BillingRollout.activated_at, Assinatura)
        .select_from(BillingRollout)
        .outerjoin(Assinatura, Assinatura.usuario_id == bindparam("billing_owner"))
        .where(BillingRollout.id == 1)
    )
).execution_options(populate_existing=True)


class AssinaturaRepo:
    async def get(self, session: AsyncSession, usuario_id: int) -> Assinatura | None:
        return cast(
            Assinatura | None,
            await session.scalar(_subscription_by_owner, {"billing_owner": usuario_id}),
        )

    async def read_status(self, session: AsyncSession, usuario_id: int) -> BillingReadModel:
        # Cache SQL construction, never entitlements or rows. Every call reads
        # the current database clock, rollout and owned subscription in one trip.
        now, rollout_at, assinatura = (
            await session.execute(_live_billing_status, {"billing_owner": usuario_id})
        ).one()
        snapshot = (
            None
            if assinatura is None
            else BillingSnapshot(
                status=SubscriptionStatus(assinatura.status),
                trial_started_at=assinatura.trial_started_at,
                trial_ends_at=assinatura.trial_ends_at,
                current_period_started_at=assinatura.current_period_started_at,
                current_period_ends_at=assinatura.current_period_ends_at,
                price_id=assinatura.price_id,
                trial_confirmed=assinatura.trial_confirmed,
            )
        )
        return read_model(snapshot, now=now, rollout_at=rollout_at)

    async def transition(
        self,
        session: AsyncSession,
        usuario_id: int,
        *,
        status: SubscriptionStatus,
        current_period_started_at: datetime | None = None,
        current_period_ends_at: datetime | None = None,
        price_id: int | None = None,
    ) -> Assinatura:
        assinatura = await session.scalar(
            select(Assinatura).where(Assinatura.usuario_id == usuario_id).with_for_update()
        )
        if assinatura is None:
            raise ValueError("trial grant is missing")
        if status == SubscriptionStatus.TRIALING and assinatura.status != status:
            raise ValueError("a second trial is forbidden")
        if (current_period_started_at is None) != (current_period_ends_at is None):
            raise ValueError("period bounds must be supplied together")
        agreed_price_id = assinatura.price_id if price_id is None else price_id
        if status == SubscriptionStatus.ACTIVE and (
            current_period_started_at is None or agreed_price_id is None
        ):
            raise ValueError("active subscription requires a period and agreed price")
        if assinatura.price_id is not None and agreed_price_id != assinatura.price_id:
            raise ValueError("changing agreed price requires an explicit migration")
        assinatura.status = status.value
        assinatura.current_period_started_at = current_period_started_at
        assinatura.current_period_ends_at = current_period_ends_at
        assinatura.price_id = agreed_price_id
        await session.flush()
        return assinatura

    async def migrate_price(
        self, session: AsyncSession, usuario_id: int, *, new_price_id: int
    ) -> Assinatura:
        assinatura = await session.scalar(
            select(Assinatura).where(Assinatura.usuario_id == usuario_id).with_for_update()
        )
        if assinatura is None or assinatura.price_id is None:
            raise ValueError("an existing agreed price is required")
        assinatura.price_id = new_price_id
        await session.flush()
        return assinatura
