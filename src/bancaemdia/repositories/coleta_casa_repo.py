from datetime import datetime

from sqlalchemy import bindparam, func, lambda_stmt, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import ColetaCasa
from bancaemdia.repositories.base import colunas

# This hot-path upsert always has the same SQL structure. Lambda SQL caches its
# construction and compilation; all tenant IDs and raw payloads stay per-call
# bound parameters, never part of the cached statement.
_collection_insert = insert(models.ColetaCasa).values(
    usuario_id=bindparam("usuario_id"),
    casa_id=bindparam("casa_id"),
    identidade=bindparam("identidade"),
    hash_conteudo=bindparam("hash_conteudo"),
    bruto_json=bindparam("bruto_json"),
)
_collection_upsert = _collection_insert.on_conflict_do_update(
    constraint="uq_coletas_casa_identidade",
    set_={
        "hash_conteudo": _collection_insert.excluded.hash_conteudo,
        "bruto_json": _collection_insert.excluded.bruto_json,
        "recebido_em": _collection_insert.excluded.recebido_em,
        "processado_em": None,
    },
    where=models.ColetaCasa.hash_conteudo.is_distinct_from(
        _collection_insert.excluded.hash_conteudo
    ),
).returning(models.ColetaCasa)
_cached_collection_upsert = lambda_stmt(lambda: _collection_upsert).execution_options(
    dml_strategy="orm"
)
_collection_identity = lambda_stmt(
    lambda: select(models.ColetaCasa).where(
        models.ColetaCasa.usuario_id == bindparam("usuario_id_1"),
        models.ColetaCasa.casa_id == bindparam("casa_id_1"),
        models.ColetaCasa.identidade == bindparam("identidade_1"),
    )
)
_legacy_daily_count = select(func.count()).where(
    models.ColetaCasa.usuario_id == bindparam("usuario_id_1"),
    models.ColetaCasa.recebido_em >= bindparam("recebido_em_1"),
    models.ColetaCasa.v2_hash.is_(None),
)
_inbox_daily_count = select(func.count()).where(
    models.ColetaEntrega.usuario_id == bindparam("usuario_id_1"),
    models.ColetaEntrega.criada_em >= bindparam("recebido_em_1"),
    models.ColetaEntrega.ack != "rejected",
)
_daily_count_query = select(
    _legacy_daily_count.scalar_subquery()
    + _inbox_daily_count.scalar_subquery()
    + select(func.count())
    .where(
        models.NativeBetEvidence.usuario_id == bindparam("usuario_id_1"),
        models.NativeBetEvidence.created_at >= bindparam("recebido_em_1"),
    )
    .scalar_subquery()
    + select(func.count())
    .where(
        models.ReaderQuarantine.usuario_id == bindparam("usuario_id_1"),
        models.ReaderQuarantine.created_at >= bindparam("recebido_em_1"),
    )
    .scalar_subquery()
)
_daily_count = lambda_stmt(lambda: _daily_count_query)


class ColetaCasaRepo:
    async def lock_daily_admission(self, session: AsyncSession, usuario_id: int) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:resource, 0))"),
            {"resource": f"collection-daily:{usuario_id}"},
        )

    async def matching_counts(
        self, session: AsyncSession, usuario_id: int, aposta_id: int | None
    ) -> dict[str, int]:
        from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

        # The collection route already resolved this bet while holding the tenant
        # pairing lock. Reuse its ID instead of querying the same key twice.
        return (
            {}
            if aposta_id is None
            else await CruzamentoCandidatoRepo().counts(session, usuario_id, aposta_id)
        )

    async def get_by_identidade(
        self, session: AsyncSession, usuario_id: int, casa_id: int, identidade: str
    ) -> ColetaCasa | None:
        obj = (
            await session.execute(
                _collection_identity,
                {"usuario_id_1": usuario_id, "casa_id_1": casa_id, "identidade_1": identidade},
            )
        ).scalar_one_or_none()
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
        quantas: int = (
            await session.execute(
                _daily_count, {"usuario_id_1": usuario_id, "recebido_em_1": desde}
            )
        ).scalar_one()
        return quantas

    async def upsert_idempotent(
        self, session: AsyncSession, dados: dict[str, object]
    ) -> ColetaCasa | None:
        if set(dados) == {"usuario_id", "casa_id", "identidade", "hash_conteudo", "bruto_json"}:
            obj = (await session.execute(_cached_collection_upsert, dados)).scalar_one_or_none()
            return None if obj is None else ColetaCasa(**colunas(obj))
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
