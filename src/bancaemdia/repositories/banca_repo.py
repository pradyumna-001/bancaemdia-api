from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Banca
from bancaemdia.repositories.base import colunas


class BancaRepo:
    async def get_by_id(self, session: AsyncSession, usuario_id: int, id_: int) -> Banca | None:
        stmt = select(models.Banca).where(
            models.Banca.usuario_id == usuario_id, models.Banca.id == id_
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else Banca(**colunas(obj))

    async def list_by_usuario(self, session: AsyncSession, usuario_id: int) -> list[Banca]:
        stmt = (
            select(models.Banca)
            .where(models.Banca.usuario_id == usuario_id)
            .order_by(models.Banca.id)
        )
        return [Banca(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]
