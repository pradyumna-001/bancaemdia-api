from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Movimento
from bancaemdia.repositories.base import colunas


class MovimentoRepo:
    async def append(self, session: AsyncSession, dados: dict[str, object]) -> Movimento:
        obj = (
            await session.execute(
                insert(models.Movimento).values(**dados).returning(models.Movimento)
            )
        ).scalar_one()
        return Movimento(**colunas(obj))

    async def list_by_usuario(
        self,
        session: AsyncSession,
        usuario_id: int,
        desde: datetime | None = None,
        ate: datetime | None = None,
    ) -> list[Movimento]:
        stmt = select(models.Movimento).where(models.Movimento.usuario_id == usuario_id)
        if desde is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em >= desde)
        if ate is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em < ate)
        stmt = stmt.order_by(models.Movimento.ocorrido_em, models.Movimento.id)
        return [Movimento(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def list_by_conta_casa(
        self, session: AsyncSession, conta_casa_id: int
    ) -> list[Movimento]:
        stmt = (
            select(models.Movimento)
            .where(models.Movimento.conta_casa_id == conta_casa_id)
            .order_by(models.Movimento.ocorrido_em, models.Movimento.id)
        )
        return [Movimento(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def list_page(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: dict[str, object],
        pagina: int = 1,
        tamanho: int = 50,
    ) -> tuple[list[Movimento], int]:
        stmt = select(models.Movimento, func.count().over().label("total")).where(
            models.Movimento.usuario_id == usuario_id
        )
        if filtros.get("conta_casa_id") is not None:
            stmt = stmt.where(models.Movimento.conta_casa_id == filtros["conta_casa_id"])
        if filtros.get("tipo") is not None:
            stmt = stmt.where(models.Movimento.tipo == filtros["tipo"])
        if filtros.get("desde") is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em >= filtros["desde"])
        if filtros.get("ate") is not None:
            stmt = stmt.where(models.Movimento.ocorrido_em < filtros["ate"])
        stmt = (
            stmt
            .order_by(models.Movimento.ocorrido_em.desc(), models.Movimento.id.desc())
            .limit(tamanho)
            .offset((pagina - 1) * tamanho)
        )
        linhas = (await session.execute(stmt)).all()
        if linhas:
            return [Movimento(**colunas(linha[0])) for linha in linhas], int(linhas[0].total)
        if pagina == 1:
            return [], 0
        # Uma página depois do fim ainda precisa dizer quantas linhas existem; sem esta segunda
        # leitura a tela passaria a informar total zero só porque o deslocamento ficou grande.
        contagem = select(func.count()).select_from(
            stmt.limit(None).offset(None).order_by(None).subquery()
        )
        return [], int((await session.execute(contagem)).scalar_one())
