"""Billing slice of #118. Real PostgreSQL/RLS, deterministic Stripe contract stub.

No network requests, card data or production credentials are used.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.v1.usuario import collect_user_data
from bancaemdia.domain.access import AccountReadOnlyError, require_write_access
from bancaemdia.domain.billing_catalog import BillingFrequency
from bancaemdia.domain.billing_service import reconcile, subscribe, tenant
from bancaemdia.domain.registros import Usuario
from bancaemdia.integrations.billing.stripe import BillingUnavailableError
from bancaemdia.models.assinatura import Assinatura
from bancaemdia.models.billing_checkout import BillingCheckout
from bancaemdia.models.billing_event import BillingEvent
from bancaemdia.repositories.assinatura_repo import AssinaturaRepo

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


class StripeStub:
    def __init__(self, price, customer):
        from bancaemdia.config import get_settings

        self.settings = get_settings().model_copy(update={"BILLING_CURRENCIES": "BRL"})
        self.price, self.customer_ref = price, customer
        self.remote = None
        self.creations = {}
        self.timeout_once = False
        self.calls = []

    async def subscriptions(self, customer):
        return [self.remote] if self.remote else []

    async def subscription(self, ref):
        return self.remote

    async def validate_price(self, ref, **terms):
        assert ref == self.price
        assert terms == {"amount": 12345, "currency": "BRL", "frequency": "MONTHLY"}

    async def customer(self, operation):
        self.creations.setdefault("customer-" + operation, self.customer_ref)
        return self.customer_ref

    async def checkout(self, customer, price, operation, *, trial):
        self.calls.append((operation, trial))
        response = self.creations.setdefault(
            operation,
            {
                "id": "cs_test_" + operation,
                "url": "https://checkout.stripe.com/c/pay/contract-fixture",
                "status": "open",
            },
        )
        if self.timeout_once:
            self.timeout_once = False
            raise BillingUnavailableError("billing_provider_unavailable")
        return response

    async def request(self, method, path, *args, **kwargs):
        return next(
            v
            for v in self.creations.values()
            if isinstance(v, dict) and v["id"] == path.split("/")[-1]
        )

    def confirm(self, operation, *, start=None):
        start = int(datetime.now(UTC).timestamp()) if start is None else start
        self.remote = {
            "id": "sub_" + operation,
            "livemode": False,
            "customer": self.customer_ref,
            "metadata": {"billing_operation": operation},
            "status": "trialing",
            "trial_start": start,
            "trial_end": start + 604800,
            "default_payment_method": {"type": "card", "customer": self.customer_ref},
            "items": {"data": [{"quantity": 1, "price": {"id": self.price}}]},
            "latest_invoice": None,
            "cancel_at_period_end": False,
        }


async def setup(session):
    uid = uuid4().int % 2**60
    await tenant(session, uid)
    await session.execute(
        text("INSERT INTO usuarios (id,email,nome) VALUES (:id,:email,'Billing fixture')"),
        {"id": uid, "email": f"{uid}@test.invalid"},
    )
    await session.execute(text("SELECT billing_activate_rollout()"))
    # Isolated parent transaction, so no public price escapes this fixture.
    await session.execute(text("UPDATE billing_prices SET published=false WHERE published"))
    ref = "price_fixture_" + str(uid)
    await session.execute(
        text(
            "INSERT INTO billing_prices (amount_cents,currency,frequency,valid_from,provider_plan_ref,published) VALUES (12345,'BRL','MONTHLY',now() - interval '1 minute',:ref,true)"
        ),
        {"ref": ref},
    )
    return uid, StripeStub(ref, "cus_fixture_" + str(uid))


async def test_timeout_retry_card_confirmation_and_no_repeat_trial(engine_admin):
    async with engine_admin.connect() as conn:
        transaction = await conn.begin()
        try:
            async with AsyncSession(
                conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as session:
                uid, provider = await setup(session)
                assert (await AssinaturaRepo().read_status(session, uid)).access == "READ_ONLY"
                with pytest.raises(AccountReadOnlyError):
                    await require_write_access(session, uid)
                provider.timeout_once = True
                with pytest.raises(BillingUnavailableError):
                    await subscribe(
                        session,
                        uid,
                        provider,
                        currency="BRL",
                        frequency=BillingFrequency.MONTHLY,
                        request_key="fixture-operation",
                    )
                await session.rollback()
                url = await subscribe(
                    session,
                    uid,
                    provider,
                    currency="BRL",
                    frequency=BillingFrequency.MONTHLY,
                    request_key="fixture-operation",
                )
                assert url.startswith("https://checkout.stripe.com/")
                assert len(provider.creations) == 2  # one customer, one checkout
                assert provider.calls[0] == provider.calls[1]
                await tenant(session, uid)
                op = await session.get(BillingCheckout, uid)
                # URL alone never grants access.
                assert (await AssinaturaRepo().read_status(session, uid)).access == "READ_ONLY"
                provider.confirm(op.operation)
                await reconcile(session, uid, provider)
                await session.commit()
                await tenant(session, uid)
                row = await session.get(Assinatura, uid, populate_existing=True)
                original = row.trial_started_at, row.trial_ends_at
                assert row.trial_confirmed
                assert original[1] - original[0] == timedelta(days=7)
                assert (await AssinaturaRepo().read_status(session, uid)).access == "FULL_WRITE"
                # Replayed/deleted/old events all re-fetch the same latest remote state.
                provider.remote["status"] = "canceled"
                await reconcile(session, uid, provider)
                await reconcile(session, uid, provider)
                assert (row.trial_started_at, row.trial_ends_at) == original
                with pytest.raises(BillingUnavailableError):
                    await subscribe(
                        session,
                        uid,
                        provider,
                        currency="BRL",
                        frequency=BillingFrequency.MONTHLY,
                        request_key="second-operation",
                    )
                assert len(provider.creations) == 2
        finally:
            await transaction.rollback()


async def test_expired_account_db_guard_export_and_rls(engine_admin):
    async with engine_admin.connect() as conn:
        trans = await conn.begin()
        try:
            async with AsyncSession(
                conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as session:
                uid, provider = await setup(session)
                await subscribe(
                    session,
                    uid,
                    provider,
                    currency="BRL",
                    frequency=BillingFrequency.MONTHLY,
                    request_key="expired-fixture",
                )
                await tenant(session, uid)
                op = await session.get(BillingCheckout, uid)
                provider.confirm(op.operation, start=int(datetime.now(UTC).timestamp()) - 8 * 86400)
                provider.remote["status"] = "canceled"
                await reconcile(session, uid, provider)
                await session.flush()
                await session.execute(text("SET LOCAL ROLE bancaemdia_app"))
                for sql in [
                    "INSERT INTO bancas(usuario_id,nome) VALUES (:id,'blocked')",
                    "INSERT INTO eventos(usuario_id,tipo,payload_json) VALUES (:id,'APOSTA_CRIADA','{}')",
                ]:
                    nested = await session.begin_nested()
                    with pytest.raises(DBAPIError) as error:
                        await session.execute(text(sql), {"id": uid})
                    assert error.value.orig.sqlstate == "P0402"
                    await nested.rollback()
                assert (await AssinaturaRepo().read_status(session, uid)).access == "READ_ONLY"
                data = await collect_user_data(
                    session,
                    Usuario(
                        id=uid,
                        email="fixture@test.invalid",
                        nome="Fixture",
                        criado_em=datetime.now(UTC),
                        ativo=True,
                    ),
                )
                assert len(data["assinaturas"]) == 1
                assert "provider_customer_ref" not in str(data)
                await tenant(session, uid + 1)
                assert (await session.scalars(select(Assinatura))).all() == []
        finally:
            await trans.rollback()


async def test_inbox_deduplicates_at_database_constraint(engine_admin):
    from sqlalchemy.dialects.postgresql import insert

    async with engine_admin.begin() as conn:
        event_id = "evt_fixture_" + uuid4().hex
        stmt = (
            insert(BillingEvent)
            .values(id=event_id, customer_ref="cus_contract", event_type="invoice.paid")
            .on_conflict_do_nothing(index_elements=[BillingEvent.id])
        )
        assert (await conn.execute(stmt)).rowcount == 1
        assert (await conn.execute(stmt)).rowcount == 0
        await conn.execute(text("DELETE FROM billing_events WHERE id=:id"), {"id": event_id})
