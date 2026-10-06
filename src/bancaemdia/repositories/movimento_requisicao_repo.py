from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import MovimentoRequisicao
from bancaemdia.repositories.base import colunas


class MovimentoRequisicaoRepo:
    async def get(
        self,
        session: AsyncSession,
        usuario_id: int,
        chave_idempotencia: str,
    ) -> MovimentoRequisicao | None:
        stmt = select(models.MovimentoRequisicao).where(
            models.MovimentoRequisicao.usuario_id == usuario_id,
            models.MovimentoRequisicao.chave_idempotencia == chave_idempotencia,
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else MovimentoRequisicao(**colunas(obj))

    async def append(
        self,
        session: AsyncSession,
        dados: dict[str, object],
    ) -> MovimentoRequisicao:
        obj = (
            await session.execute(
                insert(models.MovimentoRequisicao)
                .values(**dados)
                .returning(models.MovimentoRequisicao)
            )
        ).scalar_one()
        return MovimentoRequisicao(**colunas(obj))
