from datetime import datetime
from typing import cast

from sqlalchemy import distinct, func, select, text, true, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bancaemdia import models
from bancaemdia.domain.registros import Aposta, ApostasPorOrigem
from bancaemdia.repositories.aposta_consolidacao import financial_predicate
from bancaemdia.repositories.base import colunas
from bancaemdia.repositories.selecao_apostas import selecionar_apostas

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
        stmt = select(models.Aposta).where(
            models.Aposta.usuario_id == usuario_id, financial_predicate()
        )
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

    async def get_by_chave_for_update(
        self, session: AsyncSession, usuario_id: int, chave: str
    ) -> Aposta | None:
        from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

        await CruzamentoCandidatoRepo().lock(session, usuario_id)
        obj = (
            await session.execute(
                select(models.Aposta)
                .where(models.Aposta.usuario_id == usuario_id, models.Aposta.chave == chave)
                .with_for_update()
            )
        ).scalar_one_or_none()
        return None if obj is None else Aposta(**colunas(obj))

    async def list_page(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: dict[str, object],
        pagina: int = 1,
        tamanho: int = 50,
    ) -> tuple[list[Aposta], int]:
        base = selecionar_apostas(usuario_id, filtros).cte("apostas_filtradas")
        aposta = aliased(models.Aposta, base)
        pagina_sql = (
            select(aposta)
            .order_by(aposta.criada_em.desc(), aposta.id.desc())
            .limit(tamanho)
            .offset((pagina - 1) * tamanho)
            .subquery()
        )
        item = aliased(models.Aposta, pagina_sql)
        total_sql = select(func.count().label("total")).select_from(base).subquery()
        # One statement/snapshot, including an empty page beyond the last result.
        stmt = (
            select(item, total_sql.c.total)
            .select_from(total_sql)
            .outerjoin(pagina_sql, true())
            .order_by(item.criada_em.desc(), item.id.desc())
        )
        linhas = (await session.execute(stmt)).all()
        return [Aposta(**colunas(row[0])) for row in linhas if row[0] is not None], int(
            linhas[0].total
        )

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
        from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

        await CruzamentoCandidatoRepo().lock(session, cast(int, dados["usuario_id"]))
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
        # Replays can carry their source timestamp. A stale replay must not replace a newer row;
        # live materialization uses the database clock when the source has no timestamp.
        from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

        await CruzamentoCandidatoRepo().lock(session, cast(int, dados["usuario_id"]))
        valores = {**dados, "atualizada_em": dados.get("atualizada_em", func.clock_timestamp())}
        stmt = insert(models.Aposta).values(**valores)
        mutaveis = {
            campo: getattr(stmt.excluded, campo)
            for campo in dados
            if campo not in IMUTAVEIS and campo != "atualizada_em"
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
        from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

        await CruzamentoCandidatoRepo().lock(session, usuario_id)
        stmt = (
            update(models.Aposta)
            .where(models.Aposta.usuario_id == usuario_id, models.Aposta.chave == chave)
            .values(estado=estado, retorno_centavos=retorno_centavos, atualizada_em=func.now())
            .returning(models.Aposta)
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else Aposta(**colunas(obj))
