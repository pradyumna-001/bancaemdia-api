"""Explicit revocation erases queued private content within the caller's transaction."""

from datetime import UTC, datetime

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.models import RascunhoAposta, TelegramInbox, TelegramLink, TelegramOutbox
from bancaemdia.models.rascunho_aposta import ACTIVE_DRAFT_STATUSES
from bancaemdia.models.telegram_media import TelegramMedia
from bancaemdia.repositories.rascunho_aposta import RascunhoApostaRepo


async def forget_pending_content(session: AsyncSession, user_id: int) -> None:
    links = (
        await session.scalars(select(TelegramLink).where(TelegramLink.usuario_id == user_id))
    ).all()
    await session.execute(text("SELECT set_config('app.telegram_transport', 'on', true)"))
    for link in links:
        await session.execute(
            update(TelegramInbox)
            .where(
                TelegramInbox.sender_user_id == link.telegram_user_id,
                TelegramInbox.chat_id == link.telegram_chat_id,
            )
            .values(
                payload_ciphertext=None,
                sender_user_id=None,
                chat_id=None,
                message_id=None,
                status="DONE",
                processed_at=datetime.now(UTC),
            )
        )
    await session.execute(
        update(TelegramOutbox)
        .where(TelegramOutbox.usuario_id == user_id)
        .values(
            payload_ciphertext=b"",
            chat_id=0,
            status="DLQ",
            last_error_code="unlinked",
            lease_token=None,
            lease_until=None,
        )
    )
    await session.execute(text("SELECT set_config('app.telegram_transport', 'off', true)"))
    drafts = (
        await session.scalars(
            select(RascunhoAposta)
            .where(
                RascunhoAposta.usuario_id == user_id,
                RascunhoAposta.status.in_(ACTIVE_DRAFT_STATUSES),
            )
            .with_for_update()
        )
    ).all()
    for draft in drafts:
        await RascunhoApostaRepo().close(session, draft, "CANCELLED")
    # Cancellation remains legal for read-only subscriptions. Normal retention
    # scrubs draft content; media is detached immediately on explicit unlink.
    await session.execute(delete(TelegramMedia).where(TelegramMedia.usuario_id == user_id))
