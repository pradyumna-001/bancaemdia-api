from sqlalchemy import bindparam, lambda_stmt, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models

_house_by_name = lambda_stmt(
    lambda: select(models.Casa.id).where(models.Casa.nome == bindparam("nome_1"))
)


class CasaRepo:
    async def get_id_by_nome(self, session: AsyncSession, nome: str) -> int | None:
        casa_id: int | None = (
            await session.execute(_house_by_name, {"nome_1": nome})
        ).scalar_one_or_none()
        return casa_id

    async def get_nome_by_id(self, session: AsyncSession, casa_id: int) -> str | None:
        stmt = select(models.Casa.nome).where(models.Casa.id == casa_id)
        nome: str | None = (await session.execute(stmt)).scalar_one_or_none()
        return nome
