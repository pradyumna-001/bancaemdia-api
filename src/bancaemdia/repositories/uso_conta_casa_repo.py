from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models


class UsoContaCasaRepo:
    async def current_for_update(
        self, session: AsyncSession, usuario_id: int, casa_id: int
    ) -> models.UsoContaCasa | None:
        return (
            await session.execute(
                select(models.UsoContaCasa)
                .where(
                    models.UsoContaCasa.usuario_id == usuario_id,
                    models.UsoContaCasa.casa_id == casa_id,
                    models.UsoContaCasa.vigente_ate.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def list_by_house(
        self, session: AsyncSession, usuario_id: int, casa_id: int
    ) -> list[models.UsoContaCasa]:
        return list(
            (
                await session.execute(
                    select(models.UsoContaCasa)
                    .where(
                        models.UsoContaCasa.usuario_id == usuario_id,
                        models.UsoContaCasa.casa_id == casa_id,
                    )
                    .order_by(models.UsoContaCasa.vigente_de, models.UsoContaCasa.id)
                )
            ).scalars()
        )

    async def close(self, session: AsyncSession, usage_id: int, effective_at: datetime) -> None:
        await session.execute(
            update(models.UsoContaCasa)
            .where(models.UsoContaCasa.id == usage_id)
            .values(vigente_ate=effective_at)
        )

    async def open(
        self,
        session: AsyncSession,
        usuario_id: int,
        casa_id: int,
        conta_casa_id: int,
        effective_at: datetime,
    ) -> models.UsoContaCasa:
        return (
            await session.execute(
                insert(models.UsoContaCasa)
                .values(
                    usuario_id=usuario_id,
                    casa_id=casa_id,
                    conta_casa_id=conta_casa_id,
                    vigente_de=effective_at,
                    origem="EXPLICITA",
                )
                .returning(models.UsoContaCasa)
            )
        ).scalar_one()
