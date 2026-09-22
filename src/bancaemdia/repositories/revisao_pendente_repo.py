from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import RevisaoPendente
from bancaemdia.repositories.base import colunas


class RevisaoPendenteRepo:
    async def create(self, session: AsyncSession, dados: dict[str, object]) -> RevisaoPendente:
        obj = (
            await session.execute(
                insert(models.RevisaoPendente).values(**dados).returning(models.RevisaoPendente)
            )
        ).scalar_one()
        return RevisaoPendente(**colunas(obj))

    async def resolve_superseded(
        self, session: AsyncSession, usuario_id: int, aposta_chave: str, motivo: str | None
    ) -> int:
        stmt = update(models.RevisaoPendente).where(
            models.RevisaoPendente.usuario_id == usuario_id,
            models.RevisaoPendente.extracao_bruta["aposta_chave"].astext == aposta_chave,
            models.RevisaoPendente.resolvido_em.is_(None),
        )
        if motivo is not None:
            stmt = stmt.where(models.RevisaoPendente.motivo != motivo)
        stmt = stmt.values(resolvido_em=func.now()).returning(models.RevisaoPendente.id)
        return len(list((await session.execute(stmt)).scalars()))

    async def has_open(
        self, session: AsyncSession, usuario_id: int, aposta_chave: str, motivo: str
    ) -> bool:
        stmt = (
            select(models.RevisaoPendente.id)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.motivo == motivo,
                models.RevisaoPendente.resolvido_em.is_(None),
                models.RevisaoPendente.extracao_bruta["aposta_chave"].astext == aposta_chave,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def list_by_usuario(
        self, session: AsyncSession, usuario_id: int, apenas_abertas: bool = True
    ) -> list[RevisaoPendente]:
        stmt = select(models.RevisaoPendente).where(models.RevisaoPendente.usuario_id == usuario_id)
        if apenas_abertas:
            stmt = stmt.where(models.RevisaoPendente.resolvido_em.is_(None))
        stmt = stmt.order_by(models.RevisaoPendente.criado_em, models.RevisaoPendente.id)
        return [RevisaoPendente(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def list_abertas_by_aposta_chave(
        self, session: AsyncSession, usuario_id: int, aposta_chave: str
    ) -> list[RevisaoPendente]:
        stmt = (
            select(models.RevisaoPendente)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.resolvido_em.is_(None),
                models.RevisaoPendente.extracao_bruta["aposta_chave"].astext == aposta_chave,
            )
            .order_by(models.RevisaoPendente.criado_em, models.RevisaoPendente.id)
        )
        return [RevisaoPendente(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def resolve(
        self, session: AsyncSession, usuario_id: int, id_: int
    ) -> RevisaoPendente | None:
        stmt = (
            update(models.RevisaoPendente)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.id == id_,
                models.RevisaoPendente.resolvido_em.is_(None),
            )
            .values(resolvido_em=func.now())
            .returning(models.RevisaoPendente)
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else RevisaoPendente(**colunas(obj))
