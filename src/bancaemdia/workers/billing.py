"""DB inbox polling and periodic reconciliation, independent of webhook delivery."""

import asyncio
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.domain.billing_checkout import tenant
from bancaemdia.domain.billing_sync import reconcile
from bancaemdia.integrations.billing.stripe import BillingUnavailableError, StripeBilling
from bancaemdia.models.assinatura import Assinatura
from bancaemdia.models.billing_event import BillingEvent
from bancaemdia.workers.celery_app import app

MAX_ATTEMPTS = 8


async def reconcile_batch(engine: AsyncEngine, provider: StripeBilling) -> dict[str, int]:
    counts = {"reconciled": 0, "failed": 0, "dead": 0}
    async with AsyncSession(engine, expire_on_commit=False) as session:
        users = (
            (await session.execute(text("SELECT usuario_id FROM billing_accounts_to_reconcile()")))
            .scalars()
            .all()
        )
        await session.rollback()
        for uid in users:
            await tenant(session, uid)
            row = await session.scalar(
                select(Assinatura)
                .where(Assinatura.usuario_id == uid)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                await session.rollback()
                continue
            now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
            events = (
                await session.scalars(
                    select(BillingEvent)
                    .where(
                        BillingEvent.customer_ref == row.provider_customer_ref,
                        BillingEvent.state == "pending",
                        BillingEvent.next_attempt_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            event_ids = [e.id for e in events]
            try:
                await reconcile(session, uid, provider)
                for event in events:
                    event.state = "done"
                    event.attempts += 1
                    event.last_error = None
                await session.commit()
                counts["reconciled"] += 1
            except BillingUnavailableError:
                await session.rollback()
                await tenant(session, uid)
                # No raw provider error or payload is persisted. Updating the scan
                # timestamp prevents a broken account starving later accounts.
                await session.execute(
                    update(Assinatura)
                    .where(Assinatura.usuario_id == uid)
                    .values(last_reconciled_at=func.clock_timestamp())
                )
                failures = (
                    await session.scalars(
                        select(BillingEvent)
                        .where(BillingEvent.id.in_(event_ids), BillingEvent.state == "pending")
                        .with_for_update()
                    )
                ).all()
                for event in failures:
                    event.attempts += 1
                    event.last_error = "provider_reconciliation_failed"
                    event.next_attempt_at = now + timedelta(
                        seconds=min(3600, 30 * 2**event.attempts)
                    )
                    if event.attempts >= MAX_ATTEMPTS:
                        event.state = "dead"
                        counts["dead"] += 1
                await session.commit()
                counts["failed"] += 1
        # Unmapped customers can't grant access. Keep evidence, then dead-letter
        # instead of retaining a permanently invisible pending event.
        await session.execute(
            update(BillingEvent)
            .where(
                BillingEvent.state == "pending",
                BillingEvent.received_at < func.now() - text("interval '3 days'"),
            )
            .values(state="dead", last_error="unresolved_delivery")
        )
        await session.commit()
    return counts


@app.task(name="billing.reconcile", ignore_result=True)  # type: ignore[untyped-decorator]
def reconcile_billing() -> dict[str, int]:
    from bancaemdia.workers.materialization import get_engine

    if not get_settings().BILLING_ENABLED:
        return {"reconciled": 0, "failed": 0, "dead": 0}
    return asyncio.run(reconcile_batch(get_engine(), StripeBilling(get_settings())))
