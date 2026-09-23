from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models


class TitularRepo:
    async def create(self, session: AsyncSession, usuario_id: int, nome: str) -> models.Titular:
        return (
            await session.execute(
                insert(models.Titular)
                .values(usuario_id=usuario_id, nome=nome.strip())
                .returning(models.Titular)
            )
        ).scalar_one()

    async def get_by_id(
        self, session: AsyncSession, usuario_id: int, titular_id: int
    ) -> models.Titular | None:
        return (
            await session.execute(
                select(models.Titular).where(
                    models.Titular.usuario_id == usuario_id,
                    models.Titular.id == titular_id,
                )
            )
        ).scalar_one_or_none()

    async def archive(
        self, session: AsyncSession, usuario_id: int, titular_id: int
    ) -> models.Titular | None:
        return (
            await session.execute(
                update(models.Titular)
                .where(
                    models.Titular.usuario_id == usuario_id,
                    models.Titular.id == titular_id,
                )
                .values(arquivado=True)
                .returning(models.Titular)
            )
        ).scalar_one_or_none()
