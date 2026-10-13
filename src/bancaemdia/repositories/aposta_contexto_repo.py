"""Authorized current labels for the account/bank references persisted on a bet.

Reading never reassigns accounts: the write/materialization path owns the canonical
game-time resolution (and the explicit multicontas exception).
"""

from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models


class ApostaContextoRepo:
    async def list_by_ids(
        self, session: AsyncSession, usuario_id: int, ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        bet, account, holder, bank = (
            models.Aposta,
            models.ContaCasa,
            models.Titular,
            models.Banca,
        )
        stmt = (
            select(bet.id, account, holder, bank)
            .select_from(bet)
            .outerjoin(
                account, and_(account.id == bet.conta_casa_id, account.usuario_id == usuario_id)
            )
            .outerjoin(
                holder, and_(holder.id == account.titular_id, holder.usuario_id == usuario_id)
            )
            .outerjoin(bank, and_(bank.id == bet.banca_id, bank.usuario_id == usuario_id))
            .where(bet.usuario_id == usuario_id, bet.id.in_(ids))
        )
        return {
            ident: {
                "conta_contexto": None
                if account_row is None
                else {
                    "id": str(account_row.id),
                    "casa_id": str(account_row.casa_id),
                    "apelido": account_row.apelido or None,
                    "ativa": account_row.ativa,
                    "estado": account_row.estado,
                    "titular": None
                    if holder_row is None
                    else {
                        "id": str(holder_row.id),
                        "nome": holder_row.nome,
                        "arquivado": holder_row.arquivado,
                    },
                },
                "banca_contexto": None
                if bank_row is None
                else {"id": str(bank_row.id), "nome": bank_row.nome},
            }
            for ident, account_row, holder_row, bank_row in (await session.execute(stmt)).all()
        }
