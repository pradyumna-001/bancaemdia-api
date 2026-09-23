"""Real PostgreSQL coverage for rollout, tenant isolation and catalog constraints."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


def _uid() -> int:
    return uuid4().int % (2**60)


async def _tenant(conn, usuario_id: int) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(usuario_id)},
    )


async def test_rollout_is_explicit_stable_and_backfills_once(engine_admin: AsyncEngine) -> None:
    old_id, new_id = _uid(), _uid()
    async with engine_admin.connect() as conn:
        trans = await conn.begin()
        try:
            assert await conn.scalar(text("SELECT activated_at FROM billing_rollout")) is None
            await conn.execute(
                text("INSERT INTO usuarios (id, email, nome) VALUES (:id, :email, 'Old')"),
                {"id": old_id, "email": f"old-{old_id}@test.invalid"},
            )
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM assinaturas WHERE usuario_id = :id"), {"id": old_id}
                )
                == 0
            )
            first = await conn.scalar(text("SELECT billing_activate_rollout()"))
            second = await conn.scalar(text("SELECT billing_activate_rollout()"))
            assert first == second
            row = (
                await conn.execute(
                    text(
                        "SELECT trial_started_at, trial_ends_at FROM assinaturas WHERE usuario_id=:id"
                    ),
                    {"id": old_id},
                )
            ).one()
            assert row.trial_started_at == first
            assert row.trial_ends_at - row.trial_started_at == timedelta(days=7)
            await conn.execute(
                text("INSERT INTO usuarios (id, email, nome) VALUES (:id, :email, 'New')"),
                {"id": new_id, "email": f"new-{new_id}@test.invalid"},
            )
            new = (
                await conn.execute(
                    text(
                        "SELECT trial_started_at, trial_ends_at FROM assinaturas WHERE usuario_id=:id"
                    ),
                    {"id": new_id},
                )
            ).one()
            assert new.trial_started_at >= first
            assert new.trial_ends_at - new.trial_started_at == timedelta(days=7)
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM assinaturas WHERE usuario_id IN (:old, :new)"),
                    {"old": old_id, "new": new_id},
                )
                == 2
            )
        finally:
            await trans.rollback()


async def test_subscription_rls_and_trial_guard(
    engine_app: AsyncEngine,
) -> None:
    ana, bia = _uid(), _uid()
    start = datetime.now(UTC)
    async with engine_app.begin() as conn:
        for user_id in (ana, bia):
            await conn.execute(
                text("INSERT INTO usuarios (id, email, nome) VALUES (:id, :email, 'Test')"),
                {"id": user_id, "email": f"billing-{user_id}@test.invalid"},
            )
            await _tenant(conn, user_id)
            await conn.execute(
                text("""
                    INSERT INTO assinaturas
                        (usuario_id, status, trial_started_at, trial_ends_at)
                    VALUES (:id, 'TRIALING', :started, :ends)
                """),
                {"id": user_id, "started": start, "ends": start + timedelta(days=7)},
            )
    async with engine_app.begin() as conn:
        await _tenant(conn, ana)
        assert (await conn.execute(text("SELECT usuario_id FROM assinaturas"))).scalars().all() == [
            ana
        ]
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM audit_log WHERE resource_type = 'assinaturas'")
            )
            >= 1
        )
        assert (
            await conn.execute(
                text("UPDATE assinaturas SET status='CANCELED' WHERE usuario_id=:id"), {"id": bia}
            )
        ).rowcount == 0
        await conn.execute(
            text("UPDATE assinaturas SET status='CANCELED' WHERE usuario_id=:id"), {"id": ana}
        )
        await conn.execute(
            text("UPDATE usuarios SET email=:email WHERE id=:id"),
            {"email": f"renamed-{ana}@test.invalid", "id": ana},
        )
        await conn.execute(
            text(
                "UPDATE assinaturas SET provider_customer_ref='private-replacement' "
                "WHERE usuario_id=:id"
            ),
            {"id": ana},
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM assinaturas WHERE usuario_id=:id"), {"id": ana}
            )
            == 1
        )
        assert await conn.scalar(
            text("SELECT trial_ends_at FROM assinaturas WHERE usuario_id=:id"), {"id": ana}
        ) == start + timedelta(days=7)
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM audit_log WHERE resource_type='assinaturas' AND action='UPDATE'"
                )
            )
            >= 1
        )
        assert "private-replacement" not in str(
            (
                await conn.execute(
                    text("SELECT diff FROM audit_log WHERE usuario_id=:id"), {"id": ana}
                )
            ).all()
        )
    async with engine_app.connect() as conn:
        trans = await conn.begin()
        try:
            assert await conn.scalar(text("SELECT count(*) FROM assinaturas")) == 0
            with pytest.raises(DBAPIError):
                await conn.execute(
                    text(
                        "INSERT INTO assinaturas (usuario_id, status, trial_started_at, trial_ends_at) "
                        "VALUES (:id, 'TRIALING', :started, :ends)"
                    ),
                    {"id": _uid(), "started": start, "ends": start + timedelta(days=7)},
                )
        finally:
            await trans.rollback()
    async with engine_app.connect() as conn:
        trans = await conn.begin()
        try:
            await _tenant(conn, ana)
            with pytest.raises(DBAPIError):
                await conn.execute(
                    text("UPDATE assinaturas SET status='TRIALING' WHERE usuario_id=:id"),
                    {"id": ana},
                )
        finally:
            await trans.rollback()


async def test_catalog_exclusion_and_published_terms_are_immutable(
    engine_admin: AsyncEngine,
) -> None:
    start = datetime.now(UTC) + timedelta(days=3650)
    end = start + timedelta(days=1)
    async with engine_admin.connect() as conn:
        trans = await conn.begin()
        try:
            for amount, currency, frequency, until in (
                (0, "BRL", "MONTHLY", end),
                (-1, "BRL", "MONTHLY", end),
                (100, "USD", "MONTHLY", end),
                (100, "BRL", "WEEKLY", end),
                (100, "BRL", "MONTHLY", start),
            ):
                nested = await conn.begin_nested()
                with pytest.raises(DBAPIError):
                    await conn.execute(
                        text("""
                            INSERT INTO billing_prices
                                (amount_cents, currency, frequency, valid_from, valid_until)
                            VALUES (:amount, :currency, :frequency, :start, :end)
                        """),
                        {
                            "amount": amount,
                            "currency": currency,
                            "frequency": frequency,
                            "start": start,
                            "end": until,
                        },
                    )
                await nested.rollback()
            for amount in (100, 200):
                await conn.execute(
                    text("""
                        INSERT INTO billing_prices
                            (amount_cents, currency, frequency, valid_from, valid_until)
                        VALUES (:amount, 'BRL', 'MONTHLY', :start, :end)
                    """),
                    {"amount": amount, "start": start, "end": end},
                )
            ids = (
                (
                    await conn.execute(
                        text("SELECT id FROM billing_prices WHERE valid_from=:start ORDER BY id"),
                        {"start": start},
                    )
                )
                .scalars()
                .all()
            )
            await conn.execute(
                text("UPDATE billing_prices SET published=true WHERE id=:id"), {"id": ids[0]}
            )
            nested = await conn.begin_nested()
            with pytest.raises(DBAPIError):
                await conn.execute(
                    text("UPDATE billing_prices SET published=true WHERE id=:id"),
                    {"id": ids[1]},
                )
            await nested.rollback()
            nested = await conn.begin_nested()
            with pytest.raises(DBAPIError):
                await conn.execute(
                    text("UPDATE billing_prices SET amount_cents=999 WHERE id=:id"),
                    {"id": ids[0]},
                )
            await nested.rollback()
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM billing_price_audit WHERE price_id=:id"),
                    {"id": ids[0]},
                )
                == 2
            )
        finally:
            await trans.rollback()


async def test_concurrent_publication_keeps_one_price(engine_admin: AsyncEngine) -> None:
    start = datetime.now(UTC) + timedelta(days=3651, seconds=_uid() % 1000)
    end = start + timedelta(hours=1)
    async with engine_admin.begin() as conn:
        ids = []
        for amount in (101, 201):
            ids.append(
                await conn.scalar(
                    text("""
                    INSERT INTO billing_prices
                        (amount_cents, currency, frequency, valid_from, valid_until)
                    VALUES (:amount, 'BRL', 'MONTHLY', :start, :end) RETURNING id
                """),
                    {"amount": amount, "start": start, "end": end},
                )
            )

    async def publish(price_id: int) -> bool:
        try:
            async with engine_admin.begin() as conn:
                await conn.execute(
                    text("UPDATE billing_prices SET published=true WHERE id=:id"),
                    {"id": price_id},
                )
            return True
        except DBAPIError:
            return False

    assert sum(await asyncio.gather(*(publish(price_id) for price_id in ids))) == 1
    async with engine_admin.begin() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM billing_prices WHERE id IN (:a, :b) AND published"),
                {"a": ids[0], "b": ids[1]},
            )
            == 1
        )


async def test_new_price_does_not_rewrite_existing_subscription(engine_admin: AsyncEngine) -> None:
    user_id = _uid()
    start = datetime.now(UTC) + timedelta(days=4000)
    async with engine_admin.connect() as conn:
        trans = await conn.begin()
        try:
            prices = []
            for amount, valid_from in ((100, start), (200, start + timedelta(days=1))):
                prices.append(
                    await conn.scalar(
                        text("""
                            INSERT INTO billing_prices
                                (amount_cents, currency, frequency, valid_from, valid_until,
                                 published)
                            VALUES (:amount, 'BRL', 'MONTHLY', :start, :end, true) RETURNING id
                        """),
                        {
                            "amount": amount,
                            "start": valid_from,
                            "end": valid_from + timedelta(days=1),
                        },
                    )
                )
            await conn.execute(
                text("INSERT INTO usuarios (id, email, nome) VALUES (:id, :email, 'Test')"),
                {"id": user_id, "email": f"terms-{user_id}@test.invalid"},
            )
            await conn.execute(
                text("""
                    INSERT INTO assinaturas
                        (usuario_id, status, trial_started_at, trial_ends_at,
                         current_period_started_at, current_period_ends_at, price_id)
                    VALUES (:id, 'ACTIVE', :trial, :trial_end, :period, :period_end, :price)
                """),
                {
                    "id": user_id,
                    "trial": start,
                    "trial_end": start + timedelta(days=7),
                    "period": start,
                    "period_end": start + timedelta(days=30),
                    "price": prices[0],
                },
            )
            assert (
                await conn.scalar(
                    text("""
                    SELECT p.amount_cents FROM assinaturas a
                    JOIN billing_prices p ON p.id = a.price_id WHERE a.usuario_id = :id
                """),
                    {"id": user_id},
                )
                == 100
            )
            assert prices[0] != prices[1]
        finally:
            await trans.rollback()
