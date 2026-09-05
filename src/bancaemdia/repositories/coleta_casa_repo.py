from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import ColetaCasa
from bancaemdia.repositories.base import colunas


class ColetaCasaRepo:
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
