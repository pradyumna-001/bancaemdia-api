from datetime import datetime

from sqlalchemy import distinct, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Aposta, ApostasPorOrigem
from bancaemdia.repositories.base import colunas

IMUTAVEIS = frozenset({"id", "usuario_id", "chave", "criada_em"})


class ApostaRepo:
    async def get_by_chave(
        self, session: AsyncSession, usuario_id: int, chave: str
    ) -> Aposta | None:
        obj = (
            await session.execute(
                select(models.Aposta).where(
                    models.Aposta.usuario_id == usuario_id, models.Aposta.chave == chave
                )
            )
        ).scalar_one_or_none()
        return None if obj is None else Aposta(**colunas(obj))

    async def list_by_usuario(
        self,
        session: AsyncSession,
        usuario_id: int,
        estado: str | None = None,
        origem: str | None = None,
        banca_id: int | None = None,
        conta_casa_id: int | None = None,
        desde: datetime | None = None,
        ate: datetime | None = None,
        limite: int | None = None,
    ) -> list[Aposta]:
        stmt = select(models.Aposta).where(models.Aposta.usuario_id == usuario_id)
        if estado is not None:
            stmt = stmt.where(models.Aposta.estado == estado)
        if origem is not None:
            stmt = stmt.where(models.Aposta.origem == origem)
        if banca_id is not None:
            stmt = stmt.where(models.Aposta.banca_id == banca_id)
        if conta_casa_id is not None:
            stmt = stmt.where(models.Aposta.conta_casa_id == conta_casa_id)
        if desde is not None:
            stmt = stmt.where(models.Aposta.data_aposta >= desde)
        if ate is not None:
            stmt = stmt.where(models.Aposta.data_aposta < ate)
        stmt = stmt.order_by(models.Aposta.criada_em.desc(), models.Aposta.id.desc())
        if limite is not None:
            stmt = stmt.limit(limite)
        return [Aposta(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def count_by_origem(
        self,
        session: AsyncSession,
        usuario_id: int,
        desde: datetime | None = None,
        ate: datetime | None = None,
    ) -> list[ApostasPorOrigem]:
        mensagem = distinct(tuple_(models.Aposta.chat_id, models.Aposta.message_id))
        em_revisao = models.Aposta.revisao_grave.is_(True)
        stmt = select(
            models.Aposta.origem,
            func.count(),
            func.count().filter(em_revisao),
            func.count(mensagem),
            func.count(mensagem).filter(em_revisao),
        ).where(models.Aposta.usuario_id == usuario_id)
        if desde is not None:
            stmt = stmt.where(models.Aposta.data_aposta >= desde)
        if ate is not None:
            stmt = stmt.where(models.Aposta.data_aposta < ate)
        stmt = stmt.group_by(models.Aposta.origem).order_by(models.Aposta.origem)
        return [ApostasPorOrigem(*linha) for linha in (await session.execute(stmt)).all()]

    async def upsert_idempotent(self, session: AsyncSession, dados: dict[str, object]) -> Aposta:
        stmt = insert(models.Aposta).values(**dados)
        mutaveis = {
            campo: getattr(stmt.excluded, campo) for campo in dados if campo not in IMUTAVEIS
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["usuario_id", "chave"],
            index_where=text("chave IS NOT NULL"),
            set_={**mutaveis, "atualizada_em": func.now()},
        )
        obj = (await session.execute(stmt.returning(models.Aposta))).scalar_one()
        return Aposta(**colunas(obj))

    async def upsert_materializada(
        self, session: AsyncSession, dados: dict[str, object]
    ) -> Aposta | None:
        stmt = insert(models.Aposta).values(**dados, atualizada_em=func.clock_timestamp())
        mutaveis = {
            campo: getattr(stmt.excluded, campo) for campo in dados if campo not in IMUTAVEIS
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["usuario_id", "chave"],
            index_where=text("chave IS NOT NULL"),
            set_={**mutaveis, "atualizada_em": stmt.excluded.atualizada_em},
            where=stmt.excluded.atualizada_em > models.Aposta.atualizada_em,
        )
        obj = (await session.execute(stmt.returning(models.Aposta))).scalar_one_or_none()
        return None if obj is None else Aposta(**colunas(obj))

    async def update_estado(
        self,
        session: AsyncSession,
        usuario_id: int,
        chave: str,
        estado: str,
        retorno_centavos: int | None = None,
    ) -> Aposta | None:
        stmt = (
            update(models.Aposta)
            .where(models.Aposta.usuario_id == usuario_id, models.Aposta.chave == chave)
            .values(estado=estado, retorno_centavos=retorno_centavos, atualizada_em=func.now())
            .returning(models.Aposta)
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else Aposta(**colunas(obj))
