from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models


class CasaRepo:
    async def get_id_by_nome(self, session: AsyncSession, nome: str) -> int | None:
        stmt = select(models.Casa.id).where(models.Casa.nome == nome)
        casa_id: int | None = (await session.execute(stmt)).scalar_one_or_none()
        return casa_id

    async def get_nome_by_id(self, session: AsyncSession, casa_id: int) -> str | None:
        stmt = select(models.Casa.nome).where(models.Casa.id == casa_id)
        nome: str | None = (await session.execute(stmt)).scalar_one_or_none()
        return nome
