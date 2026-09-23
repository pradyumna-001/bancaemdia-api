"""Administrative price publication; the exclusion constraint is the final concurrency guard."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.billing_catalog import BillingFrequency, PriceTerms, public_price
from bancaemdia.models.billing_price import BillingPrice


class BillingCatalogRepo:
    async def create_draft(self, session: AsyncSession, terms: PriceTerms) -> BillingPrice:
        if terms.published:
            raise ValueError("create a draft before publication")
        price = BillingPrice(
            amount_cents=terms.amount_cents,
            currency=terms.currency,
            frequency=terms.frequency.value,
            valid_from=terms.valid_from,
            valid_until=terms.valid_until,
            provider_plan_ref=terms.provider_plan_ref,
            published=False,
        )
        session.add(price)
        await session.flush()
        return price

    async def current_public_price(self, session: AsyncSession) -> BillingPrice:
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise RuntimeError("database clock unavailable")
        rows = (
            await session.scalars(select(BillingPrice).where(BillingPrice.published.is_(True)))
        ).all()
        terms = [
            PriceTerms(
                amount_cents=row.amount_cents,
                currency=row.currency,
                frequency=BillingFrequency(row.frequency),
                valid_from=row.valid_from,
                valid_until=row.valid_until,
                provider_plan_ref=row.provider_plan_ref,
                published=row.published,
            )
            for row in rows
        ]
        chosen = public_price(terms, now=now)
        return rows[terms.index(chosen)]

    async def publish(self, session: AsyncSession, price_id: int) -> BillingPrice:
        # Intended for an administrative DB session; application role has SELECT-only RLS.
        await session.execute(select(func.pg_advisory_xact_lock(902026)))
        price = await session.scalar(
            select(BillingPrice).where(BillingPrice.id == price_id).with_for_update()
        )
        if price is None:
            raise ValueError("price does not exist")
        price.published = True
        await session.flush()
        return price
