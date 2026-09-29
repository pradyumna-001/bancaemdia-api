from datetime import timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bancaemdia import models
from bancaemdia.domain.cruzamento import MAX_NEIGHBORS, VERSION, WINDOW_DAYS
from bancaemdia.models.cruzamento_candidato import CruzamentoCandidato as Pair
from bancaemdia.models.cruzamento_candidato import CruzamentoEntrada as Entry


class CruzamentoCandidatoRepo:
    async def lock(self, session: AsyncSession, user: int) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"pareador:{user}"},
        )

    async def snapshot(self, session: AsyncSession, values: dict[str, Any]) -> Entry:
        stmt = insert(Entry).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["aposta_id"],
            set_={
                **{
                    k: getattr(stmt.excluded, k)
                    for k in values
                    if k not in {"aposta_id", "usuario_id"}
                },
                "atualizada_em": func.now(),
            },
        )
        return (
            await session.execute(stmt.returning(Entry).execution_options(populate_existing=True))
        ).scalar_one()

    def neighbors_query(self, entry: Entry) -> Any:
        assert entry.ocorrido_em is not None
        opposite = ["telegram", "print"] if entry.origem == "casa" else ["casa"]
        eligible = exists(
            select(models.Aposta.id).where(
                models.Aposta.id == Entry.aposta_id,
                models.Aposta.usuario_id == entry.usuario_id,
                models.Aposta.selecionada,
                models.Aposta.parceira_chave.is_(None),
                models.Aposta.duplicada_de.is_(None),
            )
        )
        return (
            select(Entry)
            .where(
                Entry.usuario_id == entry.usuario_id,
                Entry.casa == entry.casa,
                Entry.origem.in_(opposite),
                Entry.aposta_id != entry.aposta_id,
                Entry.ocorrido_em.between(
                    entry.ocorrido_em - timedelta(days=WINDOW_DAYS),
                    entry.ocorrido_em + timedelta(days=WINDOW_DAYS),
                ),
                eligible,
            )
            .order_by(Entry.ocorrido_em, Entry.aposta_id)
            .limit(MAX_NEIGHBORS + 1)
        )

    async def upsert(self, session: AsyncSession, values: dict[str, Any]) -> Pair:
        stmt = insert(Pair).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_cruzamento_par_versao",
            set_={
                **{
                    k: getattr(stmt.excluded, k)
                    for k in values
                    if k not in {"usuario_id", "casa_aposta_id", "telegram_aposta_id", "versao"}
                },
                "atualizado_em": func.now(),
            },
        )
        return (
            await session.execute(stmt.returning(Pair).execution_options(populate_existing=True))
        ).scalar_one()

    async def saturated_neighbor(self, session: AsyncSession, entry: Entry) -> bool:
        query = (
            self
            .neighbors_query(entry)
            .where(Entry.dados["search_truncated"].as_boolean().is_(True))
            .limit(1)
        )
        return await session.scalar(query) is not None

    async def demote_saturated_window(self, session: AsyncSession, entry: Entry) -> None:
        # A hidden neighbor beyond the cap may already have an exact edge. Veto it too,
        # using the same indexed time window, and process reviews in bounded pages.
        neighbors = (
            self
            .neighbors_query(entry)
            .with_only_columns(Entry.aposta_id)
            .order_by(None)
            .limit(None)
        )
        endpoint = Pair.telegram_aposta_id if entry.origem == "casa" else Pair.casa_aposta_id
        after = 0
        while True:
            rows = list(
                await session.scalars(
                    select(Pair)
                    .where(
                        Pair.usuario_id == entry.usuario_id,
                        Pair.versao == VERSION,
                        Pair.status == "exact",
                        Pair.id > after,
                        endpoint.in_(neighbors),
                    )
                    .order_by(Pair.id)
                    .limit(MAX_NEIGHBORS)
                )
            )
            if not rows:
                break
            after = rows[-1].id
            for pair in rows:
                pair.busca_truncada = True
            await self.adjudicate(session, entry.usuario_id, rows)

    async def invalidate(self, session: AsyncSession, user: int, ids: list[int]) -> None:
        reviews = (
            await session.scalars(
                update(Pair)
                .where(
                    Pair.usuario_id == user,
                    or_(Pair.casa_aposta_id.in_(ids), Pair.telegram_aposta_id.in_(ids)),
                    Pair.status != "excluded",
                )
                .values(status="excluded", atualizado_em=func.now())
                .returning(Pair.revisao_id)
            )
        ).all()
        await session.execute(
            update(models.RevisaoPendente)
            .where(
                models.RevisaoPendente.usuario_id == user,
                models.RevisaoPendente.id.in_([r for r in reviews if r is not None]),
            )
            .values(resolvido_em=func.now())
        )

    async def adjudicate(self, session: AsyncSession, user: int, candidates: list[Pair]) -> None:
        """Demote previously exact edges when a new competing edge appears on either side."""
        other = aliased(Pair)
        competing = exists(
            select(other.id).where(
                other.usuario_id == user,
                other.versao == VERSION,
                other.id != Pair.id,
                other.status.in_(["exact", "probable"]),
                or_(
                    other.casa_aposta_id == Pair.casa_aposta_id,
                    other.telegram_aposta_id == Pair.telegram_aposta_id,
                ),
            )
        )
        ids = [item.id for item in candidates]
        houses = [item.casa_aposta_id for item in candidates]
        tips = [item.telegram_aposta_id for item in candidates]
        # Only active exact edges and this bounded batch require recalculation.
        affected = (
            await session.scalars(
                select(Pair).where(
                    Pair.usuario_id == user,
                    Pair.versao == VERSION,
                    or_(
                        Pair.id.in_(ids),
                        (Pair.status == "exact")
                        & or_(Pair.casa_aposta_id.in_(houses), Pair.telegram_aposta_id.in_(tips)),
                    ),
                )
            )
        ).all()
        for pair in affected:
            if pair.classe_base == "incompatible":
                pair.status = "incompatible"
            else:
                competition = await session.scalar(select(competing).where(Pair.id == pair.id))
                pair.status = (
                    "exact"
                    if pair.classe_base == "exact" and not pair.busca_truncada and not competition
                    else "probable"
                )
            pair.explicacao = pair.evidencia["veredicto"]["explanation"]
            if pair.status == "probable" and pair.classe_base == "exact":
                pair.explicacao += " Elegibilidade bloqueada: busca incompleta ou concorrência por uma das apostas."
            keys = {
                int(ident): key
                for ident, key in (
                    await session.execute(
                        select(models.Aposta.id, models.Aposta.chave).where(
                            models.Aposta.usuario_id == user,
                            models.Aposta.id.in_([pair.casa_aposta_id, pair.telegram_aposta_id]),
                        )
                    )
                ).all()
            }
            review_payload = {
                "candidato_id": pair.id,
                "versao": VERSION,
                "explicacao": pair.explicacao,
                "aposta_chave": keys.get(pair.telegram_aposta_id),
                "parceira_suspeita": keys.get(pair.casa_aposta_id),
            }
            if pair.status == "probable" and pair.revisao_id is None:
                review = models.RevisaoPendente(
                    usuario_id=user,
                    motivo="cruzamento_candidato",
                    extracao_bruta=review_payload,
                )
                session.add(review)
                await session.flush()
                pair.revisao_id = review.id
            elif pair.revisao_id is not None:
                await session.execute(
                    update(models.RevisaoPendente)
                    .where(
                        models.RevisaoPendente.id == pair.revisao_id,
                        models.RevisaoPendente.usuario_id == user,
                    )
                    .values(
                        extracao_bruta=review_payload,
                        resolvido_em=None if pair.status == "probable" else func.now(),
                    )
                )
        await session.flush()

    async def counts(self, session: AsyncSession, user: int, bet_id: int) -> dict[str, int]:
        rows = await session.execute(
            select(Pair.status, func.count())
            .where(
                Pair.usuario_id == user,
                Pair.versao == VERSION,
                or_(Pair.casa_aposta_id == bet_id, Pair.telegram_aposta_id == bet_id),
                Pair.status.in_(["exact", "probable"]),
            )
            .group_by(Pair.status)
        )
        return {str(status): int(count) for status, count in rows}
