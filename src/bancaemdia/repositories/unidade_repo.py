from datetime import datetime

from sqlalchemy import insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Unidade
from bancaemdia.repositories.base import colunas


class UnidadeRepo:
    async def get_vigente(
        self, session: AsyncSession, usuario_id: int, data: datetime
    ) -> Unidade | None:
        stmt = (
            select(models.Unidade)
            .where(
                models.Unidade.usuario_id == usuario_id,
                models.Unidade.vigente_de <= data,
                or_(models.Unidade.vigente_ate.is_(None), models.Unidade.vigente_ate > data),
            )
            .order_by(models.Unidade.vigente_de.desc(), models.Unidade.id.desc())
            .limit(1)
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else Unidade(**colunas(obj))

    async def create(self, session: AsyncSession, dados: dict[str, object]) -> Unidade:
        obj = (
            await session.execute(insert(models.Unidade).values(**dados).returning(models.Unidade))
        ).scalar_one()
        return Unidade(**colunas(obj))
