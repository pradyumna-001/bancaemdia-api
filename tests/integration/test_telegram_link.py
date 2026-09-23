"""PostgreSQL checks for single-use links, RLS, throttling and revocation."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bancaemdia.models import TelegramLink, TelegramLinkCode
from bancaemdia.services.telegram_link import (
    CodeRateLimitError,
    IncomingCommand,
    get_link,
    issue_code,
    redeem_command,
    resolve_sender,
    revoke_link,
)


@pytest.mark.asyncio
async def test_single_use_relink_revocation_and_tenant_rls(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    alice, bob = await novo_usuario(), await novo_usuario()
    async with como(engine_app, alice) as session:
        issued = await issue_code(session, alice)
        assert len(issued.code) == 8
        assert (
            timedelta(minutes=29) < issued.expires_at - datetime.now(UTC) <= timedelta(minutes=30)
        )
        await session.commit()
    async with como(engine_app, bob) as session:
        assert (
            await session.scalar(
                select(TelegramLinkCode).where(TelegramLinkCode.usuario_id == alice)
            )
            is None
        )
        assert await get_link(session, bob) is None
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("group", 880001, -100, f"/vincular {issued.code}")
            )
            is False
        )
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880001, 880001, f"/vincular {issued.code}")
            )
            is True
        )
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880002, 880002, f"/vincular {issued.code}")
            )
            is False
        )
    async with como(engine_app, bob) as session:
        assert (
            await session.scalar(select(TelegramLink).where(TelegramLink.usuario_id == alice))
            is None
        )
    async with como(engine_app, None) as session:
        assert await resolve_sender(session, 880001, 880001) == alice
        await session.commit()
    async with como(engine_app, alice) as session:
        assert await get_link(session, alice) is not None
        pending = await issue_code(session, alice)
        assert await revoke_link(session, alice) is True
        await session.commit()
    async with como(engine_app, None) as session:
        assert await resolve_sender(session, 880001, 880001) is None
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880007, 880007, f"/vincular {pending.code}")
            )
            is False
        )
    async with como(engine_app, alice) as session:
        fresh = await issue_code(session, alice)
        await session.commit()
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880001, 880001, f"/vincular {fresh.code}")
            )
            is True
        )
    async with como(engine_app, alice) as session:
        link = await get_link(session, alice)
        assert link is not None and link.revoked_at is None


@pytest.mark.asyncio
async def test_issue_invalidation_expiry_and_rate_limit(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user = await novo_usuario()
    async with como(engine_app, user) as session:
        first = await issue_code(session, user)
        second = await issue_code(session, user)
        third = await issue_code(session, user)
        with pytest.raises(CodeRateLimitError):
            await issue_code(session, user)
        await session.commit()
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880003, 880003, f"/vincular {first.code}")
            )
            is False
        )
    async with como(engine_app, user) as session:
        await session.execute(
            update(TelegramLinkCode)
            .where(
                TelegramLinkCode.usuario_id == user,
                TelegramLinkCode.invalidated_at.is_(None),
            )
            .values(
                issued_at=datetime.now(UTC) - timedelta(minutes=31),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880004, 880004, f"/vincular {third.code}")
            )
            is False
        )
    assert second.code != first.code


@pytest.mark.asyncio
async def test_sender_bruteforce_block_and_anonymous_audit(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user = await novo_usuario()
    async with como(engine_app, user) as session:
        issued = await issue_code(session, user)
        await session.commit()
    for _ in range(5):
        async with como(engine_app, None) as session:
            assert (
                await redeem_command(
                    session, IncomingCommand("private", 880005, 880005, "/vincular 22222222")
                )
                is False
            )
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880005, 880005, f"/vincular {issued.code}")
            )
            is False
        )
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880006, 880006, f"/vincular {issued.code}")
            )
            is True
        )
    async with como(engine_app, user) as session:
        records = (
            await session.execute(
                text(
                    "SELECT resource_type, diff::text FROM audit_log "
                    "WHERE usuario_id = :uid AND resource_type IN ('telegram_links','telegram_link_codes')"
                ),
                {"uid": user},
            )
        ).all()
        assert records
        assert issued.code not in str(records)
        assert "880006" not in str(records)
        # The anonymous failure audit and keyed throttle have no direct app-role policy.
        assert await session.scalar(text("SELECT count(*) FROM telegram_link_attempt_events")) == 0


@pytest.mark.asyncio
async def test_one_telegram_identity_cannot_bind_two_accounts(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    alice, bob = await novo_usuario(), await novo_usuario()
    async with como(engine_app, alice) as session:
        alice_code = await issue_code(session, alice)
        await session.commit()
    async with como(engine_app, bob) as session:
        bob_code = await issue_code(session, bob)
        await session.commit()
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880008, 880008, f"/vincular {alice_code.code}")
            )
            is True
        )
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880008, 880008, f"/vincular {bob_code.code}")
            )
            is False
        )
    async with como(engine_app, bob) as session:
        assert await get_link(session, bob) is None
    async with como(engine_app, None) as session:
        assert (
            await redeem_command(
                session, IncomingCommand("private", 880009, 880009, f"/vincular {bob_code.code}")
            )
            is True
        )
