from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Usuario
from bancaemdia.repositories.base import colunas


class UsuarioRepo:
    async def get_by_id(self, session: AsyncSession, id_: int) -> Usuario | None:
        obj = (
            await session.execute(select(models.Usuario).where(models.Usuario.id == id_))
        ).scalar_one_or_none()
        return None if obj is None else Usuario(**colunas(obj))

    async def get_by_email(self, session: AsyncSession, email: str) -> Usuario | None:
        obj = (
            await session.execute(
                select(models.Usuario).where(func.lower(models.Usuario.email) == email.lower())
            )
        ).scalar_one_or_none()
        return None if obj is None else Usuario(**colunas(obj))

    async def list_active_ids(
        self, session: AsyncSession, depois_de: int = 0, limite: int = 1000
    ) -> list[int]:
        stmt = (
            select(models.Usuario.id)
            .where(models.Usuario.id > depois_de, models.Usuario.ativo.is_(True))
            .order_by(models.Usuario.id)
            .limit(limite)
        )
        return list((await session.execute(stmt)).scalars())

    async def create(self, session: AsyncSession, email: str, nome: str) -> Usuario:
        obj = (
            await session.execute(
                insert(models.Usuario).values(email=email, nome=nome).returning(models.Usuario)
            )
        ).scalar_one()
        return Usuario(**colunas(obj))
