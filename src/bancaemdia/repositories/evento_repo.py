from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Evento
from bancaemdia.repositories.base import colunas


class EventoRepo:
    async def list_by_aposta_chaves(
        self, session: AsyncSession, usuario_id: int, chaves: list[str]
    ) -> dict[str, list[Evento]]:
        """Read only the page's histories, in the canonical per-bet event order."""
        if not chaves:
            return {}
        stmt = (
            select(models.Evento)
            .where(models.Evento.usuario_id == usuario_id, models.Evento.aposta_chave.in_(chaves))
            .order_by(models.Evento.id)
        )
        histories: dict[str, list[Evento]] = {}
        for obj in (await session.execute(stmt)).scalars():
            event = Evento(**colunas(obj))
            assert event.aposta_chave is not None
            histories.setdefault(event.aposta_chave, []).append(event)
        return histories

    async def append(self, session: AsyncSession, dados: dict[str, object]) -> Evento:
        obj = (
            await session.execute(insert(models.Evento).values(**dados).returning(models.Evento))
        ).scalar_one()
        return Evento(**colunas(obj))

    async def list_by_aposta_chave(
        self, session: AsyncSession, usuario_id: int, chave: str
    ) -> list[Evento]:
        stmt = (
            select(models.Evento)
            .where(models.Evento.usuario_id == usuario_id, models.Evento.aposta_chave == chave)
            .order_by(models.Evento.id)
        )
        return [Evento(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def list_by_usuario(
        self, session: AsyncSession, usuario_id: int, limite: int | None = None
    ) -> list[Evento]:
        stmt = (
            select(models.Evento)
            .where(models.Evento.usuario_id == usuario_id)
            .order_by(models.Evento.criado_em.desc(), models.Evento.id.desc())
        )
        if limite is not None:
            stmt = stmt.limit(limite)
        return [Evento(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]
