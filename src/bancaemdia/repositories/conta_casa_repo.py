from datetime import datetime

from sqlalchemy import insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import ContaCasa
from bancaemdia.repositories.base import colunas


class ContaCasaRepo:
    async def get_by_usuario_casa(
        self, session: AsyncSession, usuario_id: int, casa_id: int
    ) -> ContaCasa | None:
        stmt = (
            select(models.ContaCasa)
            .where(
                models.ContaCasa.usuario_id == usuario_id,
                models.ContaCasa.casa_id == casa_id,
                models.ContaCasa.ativa.is_(True),
            )
            .limit(2)
        )
        matches = list((await session.execute(stmt)).scalars())
        return ContaCasa(**colunas(matches[0])) if len(matches) == 1 else None

    async def get_by_id(self, session: AsyncSession, usuario_id: int, id_: int) -> ContaCasa | None:
        stmt = select(models.ContaCasa).where(
            models.ContaCasa.usuario_id == usuario_id, models.ContaCasa.id == id_
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else ContaCasa(**colunas(obj))

    async def get_many_for_update(
        self, session: AsyncSession, usuario_id: int, ids: list[int]
    ) -> list[ContaCasa]:
        # Toda transferência trava as contas na mesma ordem, mesmo que a chamada as tenha recebido
        # ao contrário. Isso evita que duas transferências cruzadas esperem uma pela outra.
        ids_ordenados = sorted(set(ids))
        stmt = (
            select(models.ContaCasa)
            .where(
                models.ContaCasa.usuario_id == usuario_id,
                models.ContaCasa.id.in_(ids_ordenados),
            )
            .order_by(models.ContaCasa.id)
            .with_for_update()
        )
        return [ContaCasa(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def list_by_usuario(self, session: AsyncSession, usuario_id: int) -> list[ContaCasa]:
        stmt = (
            select(models.ContaCasa)
            .where(models.ContaCasa.usuario_id == usuario_id)
            .order_by(models.ContaCasa.id)
        )
        return [ContaCasa(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def get_vigente_by_nome_da_casa(
        self, session: AsyncSession, usuario_id: int, nome: str, data: datetime | None = None
    ) -> ContaCasa | None:
        stmt = (
            select(models.ContaCasa)
            .join(models.Casa, models.Casa.id == models.ContaCasa.casa_id)
            .where(
                models.ContaCasa.usuario_id == usuario_id,
                models.Casa.nome == nome,
                models.ContaCasa.ativa.is_(True),
            )
            .limit(2)
        )
        if data is not None:
            stmt = stmt.where(
                or_(models.ContaCasa.desde.is_(None), models.ContaCasa.desde <= data),
                or_(models.ContaCasa.ate.is_(None), models.ContaCasa.ate >= data),
            )
        matches = list((await session.execute(stmt)).scalars())
        return ContaCasa(**colunas(matches[0])) if len(matches) == 1 else None

    async def create(self, session: AsyncSession, dados: dict[str, object]) -> ContaCasa:
        obj = (
            await session.execute(
                insert(models.ContaCasa).values(**dados).returning(models.ContaCasa)
            )
        ).scalar_one()
        return ContaCasa(**colunas(obj))

    async def update_ate(
        self, session: AsyncSession, usuario_id: int, id_: int, ate: datetime | None
    ) -> ContaCasa | None:
        stmt = (
            update(models.ContaCasa)
            .where(models.ContaCasa.usuario_id == usuario_id, models.ContaCasa.id == id_)
            .values(ate=ate)
            .returning(models.ContaCasa)
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else ContaCasa(**colunas(obj))

    async def update_banca(
        self, session: AsyncSession, usuario_id: int, id_: int, banca_id: int | None
    ) -> ContaCasa | None:
        stmt = (
            update(models.ContaCasa)
            .where(models.ContaCasa.usuario_id == usuario_id, models.ContaCasa.id == id_)
            .values(banca_id=banca_id)
            .returning(models.ContaCasa)
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else ContaCasa(**colunas(obj))
