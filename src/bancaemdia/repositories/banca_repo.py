from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Banca
from bancaemdia.repositories.base import colunas


class BancaRepo:
    async def get_by_id(self, session: AsyncSession, usuario_id: int, id_: int) -> Banca | None:
        stmt = select(models.Banca).where(
            models.Banca.usuario_id == usuario_id, models.Banca.id == id_
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else Banca(**colunas(obj))

    async def list_by_usuario(self, session: AsyncSession, usuario_id: int) -> list[Banca]:
        stmt = (
            select(models.Banca)
            .where(models.Banca.usuario_id == usuario_id)
            .order_by(models.Banca.id)
        )
        return [Banca(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def nome_em_uso(
        self, session: AsyncSession, usuario_id: int, nome: str, exceto: int | None = None
    ) -> bool:
        # A unicidade do banco vale também para arquivadas: o nome segue identificando a história.
        stmt = select(models.Banca.id).where(
            models.Banca.usuario_id == usuario_id, models.Banca.nome == nome
        )
        if exceto is not None:
            stmt = stmt.where(models.Banca.id != exceto)
        return await session.scalar(stmt.limit(1)) is not None

    async def contas_vinculadas(self, session: AsyncSession, usuario_id: int, id_: int) -> int:
        total = await session.scalar(
            select(func.count())
            .select_from(models.ContaCasa)
            .where(models.ContaCasa.usuario_id == usuario_id, models.ContaCasa.banca_id == id_)
        )
        return int(total or 0)

    async def create(
        self, session: AsyncSession, usuario_id: int, nome: str, saldo_inicial: int | None
    ) -> Banca:
        obj = (
            await session.execute(
                insert(models.Banca)
                .values(usuario_id=usuario_id, nome=nome, saldo_inicial_centavos=saldo_inicial)
                .returning(models.Banca)
            )
        ).scalar_one()
        return Banca(**colunas(obj))

    async def update(
        self, session: AsyncSession, usuario_id: int, id_: int, valores: dict[str, object]
    ) -> Banca:
        obj = (
            await session.execute(
                update(models.Banca)
                .where(models.Banca.usuario_id == usuario_id, models.Banca.id == id_)
                .values(**valores)
                .returning(models.Banca)
            )
        ).scalar_one()
        return Banca(**colunas(obj))

    async def archive(
        self, session: AsyncSession, usuario_id: int, id_: int, quando: datetime
    ) -> Banca:
        return await self.update(session, usuario_id, id_, {"arquivada_em": quando})


class BancaRequisicaoRepo:
    async def get(
        self, session: AsyncSession, usuario_id: int, chave: str
    ) -> models.BancaRequisicao | None:
        stmt = select(models.BancaRequisicao).where(
            models.BancaRequisicao.usuario_id == usuario_id,
            models.BancaRequisicao.chave == chave,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def append(self, session: AsyncSession, dados: dict[str, object]) -> None:
        await session.execute(insert(models.BancaRequisicao).values(**dados))
