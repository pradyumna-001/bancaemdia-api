"""Billing #118: real Redis delivery and Celery scheduling, isolated PostgreSQL.

Only the Stripe HTTP boundary is substituted. This test never loads a real key.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from celery import Celery
from celery.beat import Scheduler
from celery.contrib.testing.worker import start_worker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.config import get_settings
from bancaemdia.domain.billing_checkout import tenant
from bancaemdia.models.assinatura import Assinatura
from bancaemdia.models.billing_checkout import BillingCheckout
from bancaemdia.models.billing_event import BillingEvent
from bancaemdia.models.billing_price import BillingPrice
from bancaemdia.workers import billing, celery_app, materialization

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("billing-runtime")]


def test_beat_dispatches_real_redis_task_and_worker_converges_inbox(
    monkeypatch, isolated_postgres_database
):
    from testcontainers.community.redis import RedisContainer

    database = isolated_postgres_database
    with RedisContainer("redis:7-alpine") as redis:
        broker = f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"
        engine = create_async_engine(database.url_app, poolclass=NullPool)
        admin = create_async_engine(database.url_admin, poolclass=NullPool)
        uid = uuid4().int % 2**60
        operation = uuid4().hex
        customer = "cus_runtime_fixture"
        reference = "sub_runtime_fixture"
        event_id = "evt_runtime_fixture"
        now = int(datetime.now(UTC).timestamp())
        remote = {
            "id": reference,
            "customer": customer,
            "livemode": False,
            "metadata": {"billing_operation": operation},
            "status": "trialing",
            "trial_start": now,
            "trial_end": now + 604800,
            "default_payment_method": {"type": "card", "customer": customer},
            "pending_setup_intent": None,
            "items": {"data": [{"quantity": 1, "price": {"id": "price_runtime_fixture"}}]},
            "cancel_at_period_end": False,
        }

        class Provider:
            async def subscriptions(self, customer_ref):
                assert customer_ref == customer
                return [remote]

            async def subscription(self, subscription_ref):
                assert subscription_ref == reference
                return remote

            async def checkout_status(self, session_ref):
                assert session_ref == "cs_runtime_fixture"
                return {
                    "status": "complete",
                    "livemode": False,
                    "customer": customer,
                    "subscription": reference,
                }

        async def seed():
            async with AsyncSession(admin, expire_on_commit=False) as session:
                await tenant(session, uid)
                await session.execute(text("SELECT billing_activate_rollout()"))
                await session.execute(
                    text(
                        "INSERT INTO usuarios(id,email,nome) VALUES (:id,'runtime@example.invalid','Fixture')"
                    ),
                    {"id": uid},
                )
                price = BillingPrice(
                    amount_cents=12345,
                    currency="BRL",
                    frequency="MONTHLY",
                    valid_from=datetime.now(UTC),
                    provider_plan_ref="price_runtime_fixture",
                    published=True,
                )
                session.add(price)
                await session.flush()
                row = await session.get(Assinatura, uid)
                row.provider = "stripe"
                row.provider_customer_ref = customer
                session.add(
                    BillingCheckout(
                        usuario_id=uid,
                        operation=operation,
                        request_hash="runtime-fixture",
                        price_id=price.id,
                        trial=True,
                        state="pending",
                        session_ref="cs_runtime_fixture",
                    )
                )
                session.add(
                    BillingEvent(
                        id=event_id,
                        customer_ref=customer,
                        event_type="checkout.session.completed",
                    )
                )
                await session.commit()

        async def state():
            async with AsyncSession(engine) as session:
                await tenant(session, uid)
                row = await session.get(Assinatura, uid)
                event = await session.get(BillingEvent, event_id)
                return row.trial_confirmed, event.state, event.attempts

        settings = get_settings().model_copy(update={"BILLING_ENABLED": True})
        monkeypatch.setattr(billing, "get_settings", lambda: settings)
        monkeypatch.setattr(billing, "StripeBilling", lambda _: Provider())
        monkeypatch.setattr(materialization, "get_engine", lambda: engine)
        asyncio.run(seed())
        assert asyncio.run(state()) == (False, "pending", 0)

        # An isolated Celery app avoids mutating cached broker connections in
        # unrelated tests. Execute the actual production task body and route.
        app = Celery("billing-runtime-test", broker=broker, backend=broker)
        app.conf.update(
            task_routes={"billing.*": celery_app.app.conf.task_routes["billing.*"]},
            beat_schedule=celery_app.app.conf.beat_schedule,
            task_serializer="json",
            accept_content=["json"],
            worker_hijack_root_logger=False,
        )
        app.task(name="billing.reconcile", ignore_result=True)(billing.reconcile_billing.run)
        try:
            with start_worker(
                app, pool="solo", queues=["materialization"], perform_ping_check=False
            ):
                scheduler = Scheduler(app=app)
                entry = scheduler.schedule["billing-reconcile"]
                entry.last_run_at = datetime.now(UTC) - timedelta(seconds=61)
                assert entry.is_due().is_due
                scheduler.tick()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if asyncio.run(state()) == (True, "done", 1):
                        break
                    time.sleep(0.2)
                assert asyncio.run(state()) == (True, "done", 1)
                # Another real delivery scans current state but cannot consume
                # the same persisted notification a second time.
                counter = billing.metrics.reconciliations.labels(outcome="reconciled")
                before = counter._value.get()
                app.send_task("billing.reconcile")
                deadline = time.monotonic() + 20
                while counter._value.get() == before and time.monotonic() < deadline:
                    time.sleep(0.2)
                assert counter._value.get() > before
                assert asyncio.run(state()) == (True, "done", 1)
                scheduler.close()
        finally:
            app.close()
            celery_app.app.set_current()
            celery_app.app.set_default()
            asyncio.run(engine.dispose())
            asyncio.run(admin.dispose())
