"""Tenant-scoped draft persistence with conditional version updates."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.models.rascunho_aposta import (
    ACTIVE_DRAFT_STATUSES,
    RascunhoAposta,
    RascunhoCorrecao,
)


class DraftVersionConflictError(Exception):
    pass


class RascunhoApostaRepo:
    async def active(
        self, session: AsyncSession, user_id: int, chat_id: int, *, lock: bool = False
    ) -> RascunhoAposta | None:
        query = select(RascunhoAposta).where(
            RascunhoAposta.usuario_id == user_id,
            RascunhoAposta.telegram_chat_id == chat_id,
            RascunhoAposta.status.in_(ACTIVE_DRAFT_STATUSES),
        )
        if lock:
            query = query.with_for_update()
        return cast(RascunhoAposta | None, await session.scalar(query))

    async def by_origin(
        self, session: AsyncSession, user_id: int, chat_id: int, message_id: int
    ) -> RascunhoAposta | None:
        return cast(
            RascunhoAposta | None,
            await session.scalar(
                select(RascunhoAposta).where(
                    RascunhoAposta.usuario_id == user_id,
                    RascunhoAposta.telegram_chat_id == chat_id,
                    RascunhoAposta.telegram_message_id == message_id,
                )
            ),
        )

    async def create(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        update_id: int,
        media_reference_ciphertext: bytes | None,
        media_hash: str | None,
        fields: dict[str, Any],
        metadata: dict[str, Any],
        missing: list[str],
        status: str,
    ) -> tuple[RascunhoAposta, bool]:
        identifier = uuid4()
        inserted = await session.scalar(
            insert(RascunhoAposta)
            .values(
                id=identifier,
                usuario_id=user_id,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                telegram_update_id=update_id,
                media_reference_ciphertext=media_reference_ciphertext,
                media_hash=media_hash,
                fields_json=fields,
                field_meta_json=metadata,
                missing_fields_json=missing,
                status=status,
                version=1,
            )
            .on_conflict_do_nothing()
            .returning(RascunhoAposta.id)
        )
        if inserted is not None:
            item = await session.get(RascunhoAposta, inserted)
            assert item is not None
            return item, True
        existing = await self.by_origin(session, user_id, chat_id, message_id)
        if existing is None:
            existing = await self.active(session, user_id, chat_id)
        if existing is None:
            raise DraftVersionConflictError("draft changed while creating")
        return existing, False

    async def save(
        self,
        session: AsyncSession,
        draft: RascunhoAposta,
        *,
        expected_version: int,
        fields: dict[str, Any],
        metadata: dict[str, Any],
        missing: list[str],
        status: str,
        changes: dict[str, Any],
        update_id: int | None,
    ) -> RascunhoAposta:
        new_version = expected_version + 1
        saved = await session.scalar(
            update(RascunhoAposta)
            .where(
                RascunhoAposta.id == draft.id,
                RascunhoAposta.usuario_id == draft.usuario_id,
                RascunhoAposta.version == expected_version,
                RascunhoAposta.status.in_(ACTIVE_DRAFT_STATUSES),
            )
            .values(
                fields_json=fields,
                field_meta_json=metadata,
                missing_fields_json=missing,
                status=status,
                version=new_version,
                updated_at=datetime.now(UTC),
            )
            .returning(RascunhoAposta.id)
        )
        if saved is None:
            raise DraftVersionConflictError("draft version changed")
        if changes:
            session.add(
                RascunhoCorrecao(
                    usuario_id=draft.usuario_id,
                    rascunho_id=draft.id,
                    version=new_version,
                    telegram_update_id=update_id,
                    changes_json=changes,
                )
            )
            await session.flush()
        await session.refresh(draft)
        return draft

    async def close(
        self, session: AsyncSession, draft: RascunhoAposta, status: str
    ) -> RascunhoAposta:
        if status not in {"CANCELLED", "CONFIRMED", "FAILED"}:
            raise ValueError("terminal draft status required")
        saved = await session.scalar(
            update(RascunhoAposta)
            .where(
                RascunhoAposta.id == draft.id,
                RascunhoAposta.usuario_id == draft.usuario_id,
                RascunhoAposta.version == draft.version,
                RascunhoAposta.status.in_(ACTIVE_DRAFT_STATUSES),
            )
            .values(status=status, version=draft.version + 1, closed_at=datetime.now(UTC))
            .returning(RascunhoAposta.id)
        )
        if saved is None:
            raise DraftVersionConflictError("draft version changed")
        await session.refresh(draft)
        return draft

    async def history(self, session: AsyncSession, draft_id: UUID) -> list[RascunhoCorrecao]:
        return list(
            (
                await session.scalars(
                    select(RascunhoCorrecao)
                    .where(RascunhoCorrecao.rascunho_id == draft_id)
                    .order_by(RascunhoCorrecao.version)
                )
            ).all()
        )
