from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import ColetaCasa
from bancaemdia.repositories.base import colunas


class ColetaCasaRepo:
    async def lock_daily_admission(self, session: AsyncSession, usuario_id: int) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:resource, 0))"),
            {"resource": f"collection-daily:{usuario_id}"},
        )

    async def get_by_identidade(
        self, session: AsyncSession, usuario_id: int, casa_id: int, identidade: str
    ) -> ColetaCasa | None:
        stmt = select(models.ColetaCasa).where(
            models.ColetaCasa.usuario_id == usuario_id,
            models.ColetaCasa.casa_id == casa_id,
            models.ColetaCasa.identidade == identidade,
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else ColetaCasa(**colunas(obj))

    async def get_by_identidade_for_update(
        self, session: AsyncSession, usuario_id: int, casa_id: int, identidade: str
    ) -> ColetaCasa | None:
        stmt = (
            select(models.ColetaCasa)
            .where(
                models.ColetaCasa.usuario_id == usuario_id,
                models.ColetaCasa.casa_id == casa_id,
                models.ColetaCasa.identidade == identidade,
            )
            .with_for_update()
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else ColetaCasa(**colunas(obj))

    async def get_by_id(
        self, session: AsyncSession, usuario_id: int, id_: int
    ) -> ColetaCasa | None:
        stmt = select(models.ColetaCasa).where(
            models.ColetaCasa.usuario_id == usuario_id, models.ColetaCasa.id == id_
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else ColetaCasa(**colunas(obj))

    async def list_by_casa(
        self,
        session: AsyncSession,
        usuario_id: int,
        casa_id: int,
        depois_de: int = 0,
        limite: int = 1000,
    ) -> list[ColetaCasa]:
        stmt = (
            select(models.ColetaCasa)
            .where(
                models.ColetaCasa.usuario_id == usuario_id,
                models.ColetaCasa.casa_id == casa_id,
                models.ColetaCasa.id > depois_de,
            )
            .order_by(models.ColetaCasa.id)
            .limit(limite)
        )
        return [ColetaCasa(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def get_by_id_for_update(
        self, session: AsyncSession, usuario_id: int, id_: int
    ) -> ColetaCasa | None:
        stmt = (
            select(models.ColetaCasa)
            .where(models.ColetaCasa.usuario_id == usuario_id, models.ColetaCasa.id == id_)
            .with_for_update()
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else ColetaCasa(**colunas(obj))

    async def count_received_since(
        self, session: AsyncSession, usuario_id: int, desde: datetime
    ) -> int:
        legacy = select(func.count()).where(
            models.ColetaCasa.usuario_id == usuario_id,
            models.ColetaCasa.recebido_em >= desde,
            models.ColetaCasa.v2_hash.is_(None),
        )
        inbox = select(func.count()).where(
            models.ColetaEntrega.usuario_id == usuario_id,
            models.ColetaEntrega.criada_em >= desde,
            models.ColetaEntrega.ack != "rejected",
        )
        stmt = select(legacy.scalar_subquery() + inbox.scalar_subquery())
        quantas: int = (await session.execute(stmt)).scalar_one()
        return quantas

    async def upsert_idempotent(
        self, session: AsyncSession, dados: dict[str, object]
    ) -> ColetaCasa | None:
        stmt = insert(models.ColetaCasa).values(**dados)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_coletas_casa_identidade",
            set_={
                "hash_conteudo": stmt.excluded.hash_conteudo,
                "bruto_json": stmt.excluded.bruto_json,
                "recebido_em": stmt.excluded.recebido_em,
                "processado_em": None,
            },
            where=models.ColetaCasa.hash_conteudo.is_distinct_from(stmt.excluded.hash_conteudo),
        )
        obj = (await session.execute(stmt.returning(models.ColetaCasa))).scalar_one_or_none()
        return None if obj is None else ColetaCasa(**colunas(obj))

    async def set_processado(self, session: AsyncSession, usuario_id: int, id_: int) -> None:
        await session.execute(
            update(models.ColetaCasa)
            .where(models.ColetaCasa.usuario_id == usuario_id, models.ColetaCasa.id == id_)
            .values(processado_em=func.now())
        )
