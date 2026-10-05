from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models


def financial_predicate() -> Any:
    """One canonical row, independently of restoration/selection on either source."""
    relation = models.ApostaConsolidacao
    return ~exists(
        select(relation.id).where(
            relation.usuario_id == models.Aposta.usuario_id,
            relation.telegram_aposta_id == models.Aposta.id,
            relation.estado == "active",
        )
    )


def available_predicate() -> Any:
    relation = models.ApostaConsolidacao
    return ~exists(
        select(relation.id).where(
            relation.usuario_id == models.Aposta.usuario_id,
            or_(
                relation.telegram_aposta_id == models.Aposta.id,
                relation.casa_aposta_id == models.Aposta.id,
            ),
            relation.estado == "active",
        )
    )


class ApostaConsolidacaoRepo:
    async def active(
        self, session: AsyncSession, user: int, ids: list[int]
    ) -> list[models.ApostaConsolidacao]:
        relation = models.ApostaConsolidacao
        return list(
            await session.scalars(
                select(relation)
                .where(
                    relation.usuario_id == user,
                    relation.estado == "active",
                    or_(relation.casa_aposta_id.in_(ids), relation.telegram_aposta_id.in_(ids)),
                )
                .execution_options(populate_existing=True)
            )
        )

    async def history(
        self, session: AsyncSession, user: int, bet_id: int
    ) -> list[models.ApostaConsolidacao]:
        relation = models.ApostaConsolidacao
        return list(
            await session.scalars(
                select(relation)
                .where(
                    relation.usuario_id == user,
                    or_(relation.casa_aposta_id == bet_id, relation.telegram_aposta_id == bet_id),
                )
                .order_by(relation.id)
            )
        )
