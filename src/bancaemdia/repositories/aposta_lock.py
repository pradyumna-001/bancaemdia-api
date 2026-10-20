"""Shared bet write lock, always after the owner's matching lock."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo


async def try_lock_bet(session: AsyncSession, usuario_id: int, chave: str) -> bool:
    await CruzamentoCandidatoRepo().lock(session, usuario_id)
    return bool(
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:chave, 0))"),
            {"chave": f"{usuario_id}:{chave}"},
        )
    )
