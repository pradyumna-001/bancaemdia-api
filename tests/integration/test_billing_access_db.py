"""Exercise the access decision and database guard against real PostgreSQL."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.access import AccountReadOnlyError, require_write_access

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


@pytest.mark.parametrize(
    ("status", "confirmed", "trial_end", "paid_end", "allowed"),
    [
        ("TRIALING", True, "1 hour", "-1 hour", True),
        ("TRIALING", True, "-1 second", "-1 hour", False),
        ("TRIALING", False, "1 hour", "-1 hour", False),
        ("ACTIVE", True, "-1 day", "1 hour", True),
        ("ACTIVE", True, "-1 day", "-1 second", False),
        ("PAST_DUE", True, "-1 day", "1 hour", False),
        ("CANCELED", True, "-1 day", "1 hour", False),
    ],
)
async def test_trial_and_paid_boundaries_agree_in_service_and_sql(
    engine_admin, status, confirmed, trial_end, paid_end, allowed
):
    uid = uuid4().int % 2**60
    async with engine_admin.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(uid)}
            )
            await conn.execute(text("SELECT billing_activate_rollout()"))
            await conn.execute(
                text("INSERT INTO usuarios(id,email,nome) VALUES (:uid,:email,'Access fixture')"),
                {"uid": uid, "email": f"{uid}@test.invalid"},
            )
            # A confirmed grant may replace only its initial unconfirmed bounds.
            if confirmed:
                await conn.execute(
                    text(
                        "WITH bounds AS (SELECT clock_timestamp()+CAST(:trial_end AS interval) AS finish) "
                        "UPDATE assinaturas SET trial_confirmed=true, trial_ends_at=bounds.finish, "
                        "trial_started_at=bounds.finish-interval '168 hours' FROM bounds WHERE usuario_id=:uid"
                    ),
                    {"uid": uid, "trial_end": trial_end},
                )
            price = await conn.scalar(
                text(
                    "INSERT INTO billing_prices(amount_cents,currency,frequency,valid_from,published) "
                    "VALUES (12345,'BRL','MONTHLY',clock_timestamp(),false) RETURNING id"
                )
            )
            await conn.execute(
                text(
                    "UPDATE assinaturas SET status=:status, price_id=:price, "
                    "current_period_started_at=clock_timestamp()-interval '2 days', "
                    "current_period_ends_at=clock_timestamp()+CAST(:paid_end AS interval) WHERE usuario_id=:uid"
                ),
                {"uid": uid, "status": status, "paid_end": paid_end, "price": price},
            )
            async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
                if allowed:
                    await require_write_access(session, uid)
                else:
                    with pytest.raises(AccountReadOnlyError):
                        await require_write_access(session, uid)
            savepoint = await conn.begin_nested()
            try:
                write = text("INSERT INTO bancas(usuario_id,nome) VALUES (:uid,'Guard fixture')")
                if allowed:
                    await conn.execute(write, {"uid": uid})
                else:
                    with pytest.raises(DBAPIError) as error:
                        await conn.execute(write, {"uid": uid})
                    assert error.value.orig.sqlstate == "P0402"
            finally:
                await savepoint.rollback()
        finally:
            await transaction.rollback()


async def test_guard_installer_rejects_missing_tenant_column(engine_admin):
    async with engine_admin.connect() as conn:
        transaction = await conn.begin()
        try:
            if await conn.scalar(text("SELECT to_regclass('public.rascunho_correcoes')")) is None:
                await conn.execute(
                    text("CREATE TABLE public.rascunho_correcoes(id bigint, owner_id bigint)")
                )
            else:
                await conn.execute(
                    text(
                        "ALTER TABLE public.rascunho_correcoes RENAME COLUMN usuario_id TO owner_id"
                    )
                )
            with pytest.raises(DBAPIError, match="billing tenant column invalid") as error:
                await conn.execute(text("SELECT billing_install_write_guards()"))
            assert error.value.orig.sqlstate == "P0402"
        finally:
            await transaction.rollback()


async def test_missing_owner_is_denied_even_before_rollout(engine_admin):
    async with engine_admin.connect() as conn:
        transaction = await conn.begin()
        try:
            with pytest.raises(DBAPIError) as error:
                await conn.execute(
                    text("INSERT INTO bancas(usuario_id,nome) VALUES(NULL,'Missing tenant')")
                )
            assert error.value.orig.sqlstate == "P0402"
        finally:
            await transaction.rollback()
