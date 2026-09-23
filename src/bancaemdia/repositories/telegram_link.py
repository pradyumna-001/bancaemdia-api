"""Database operations for account linking; privileged lookups return only an owner ID."""

from datetime import datetime
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.models import TelegramLink, TelegramLinkCode, Usuario


class TelegramLinkRepo:
    async def lock_user(self, session: AsyncSession, user_id: int) -> Usuario | None:
        return cast(
            Usuario | None,
            await session.scalar(select(Usuario).where(Usuario.id == user_id).with_for_update()),
        )

    async def active_link(self, session: AsyncSession, user_id: int) -> TelegramLink | None:
        return cast(
            TelegramLink | None,
            await session.scalar(
                select(TelegramLink).where(
                    TelegramLink.usuario_id == user_id, TelegramLink.revoked_at.is_(None)
                )
            ),
        )

    async def pair(
        self, session: AsyncSession, user_id: int, telegram_user_id: int
    ) -> TelegramLink | None:
        return cast(
            TelegramLink | None,
            await session.scalar(
                select(TelegramLink)
                .where(
                    TelegramLink.usuario_id == user_id,
                    TelegramLink.telegram_user_id == telegram_user_id,
                )
                .with_for_update()
            ),
        )

    async def code(
        self, session: AsyncSession, user_id: int, digest: str
    ) -> TelegramLinkCode | None:
        return cast(
            TelegramLinkCode | None,
            await session.scalar(
                select(TelegramLinkCode)
                .where(
                    TelegramLinkCode.usuario_id == user_id,
                    TelegramLinkCode.code_hash == digest,
                )
                .with_for_update()
            ),
        )

    async def issued_since(self, session: AsyncSession, user_id: int, since: datetime) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(TelegramLinkCode)
                .where(
                    TelegramLinkCode.usuario_id == user_id,
                    TelegramLinkCode.issued_at >= since,
                )
            )
            or 0
        )

    async def code_issuer(self, session: AsyncSession, digest: str) -> int | None:
        return cast(
            int | None,
            await session.scalar(
                text("SELECT telegram_link_code_issuer(:digest)"), {"digest": digest}
            ),
        )

    async def active_owner(
        self, session: AsyncSession, telegram_user_id: int, chat_id: int
    ) -> int | None:
        return cast(
            int | None,
            await session.scalar(
                text("SELECT telegram_link_active_owner(:user_id, :chat_id)"),
                {"user_id": telegram_user_id, "chat_id": chat_id},
            ),
        )

    async def blocked_until(self, session: AsyncSession, digest: str) -> datetime | None:
        return cast(
            datetime | None,
            await session.scalar(
                text("SELECT telegram_link_attempt_state(:digest)"), {"digest": digest}
            ),
        )

    async def record_failure(self, session: AsyncSession, digest: str, outcome: str) -> None:
        await session.execute(
            text("SELECT telegram_link_record_failure(:digest, :outcome)"),
            {"digest": digest, "outcome": outcome},
        )

    async def clear_attempts(self, session: AsyncSession, digest: str) -> None:
        await session.execute(
            text("SELECT telegram_link_clear_attempts(:digest)"), {"digest": digest}
        )
