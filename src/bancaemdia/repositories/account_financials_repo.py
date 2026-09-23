"""Account-level aggregates using the same bet rules as the canonical panel."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from bancaemdia import models


class AccountFinancialsRepo:
    async def bets(
        self,
        session: AsyncSession,
        usuario_id: int,
        *,
        desde: datetime | None,
        ate: datetime | None,
        casa_id: int | None,
        titular_id: int | None,
        conta_casa_id: int | None,
    ) -> list[dict[str, int | None]]:
        bet = models.Aposta
        account = models.ContaCasa
        settled = bet.estado.not_in(("PENDENTE", "ANULADA"))

        def when_settled(value: Any) -> ColumnElement[Any]:
            return func.coalesce(func.sum(case((settled, value), else_=0)), 0)

        stmt = (
            select(
                bet.conta_casa_id.label("conta_casa_id"),
                func.count().label("apostas"),
                func.count().filter(bet.estado == "PENDENTE").label("pendentes"),
                func.count().filter(bet.estado == "GREEN").label("greens"),
                func.count().filter(bet.estado == "RED").label("reds"),
                when_settled(bet.stake_centavos).label("giro_centavos"),
                when_settled(
                    case(
                        (bet.freebet.is_(True), bet.valor_aposta_centavos), else_=bet.stake_centavos
                    )
                ).label("base_roi_centavos"),
                when_settled(func.coalesce(bet.retorno_centavos, 0)).label("retorno_centavos"),
                when_settled(func.coalesce(bet.retorno_centavos - bet.stake_centavos, 0)).label(
                    "lucro_centavos"
                ),
                func.coalesce(
                    func.sum(case((bet.estado == "PENDENTE", bet.stake_centavos), else_=0)), 0
                ).label("exposicao_aberta_centavos"),
            )
            .outerjoin(
                account,
                (account.id == bet.conta_casa_id) & (account.usuario_id == bet.usuario_id),
            )
            .where(
                bet.usuario_id == usuario_id,
                bet.selecionada.is_(True),
                bet.revisao_grave.is_(False),
            )
            .group_by(bet.conta_casa_id)
        )
        occurred = func.coalesce(bet.data_aposta, bet.criada_em)
        if desde is not None:
            stmt = stmt.where(occurred >= desde)
        if ate is not None:
            stmt = stmt.where(occurred < ate)
        if casa_id is not None:
            stmt = stmt.where(account.casa_id == casa_id)
        if titular_id is not None:
            stmt = stmt.where(account.titular_id == titular_id)
        if conta_casa_id is not None:
            stmt = stmt.where(bet.conta_casa_id == conta_casa_id)
        return [dict(row) for row in (await session.execute(stmt)).mappings()]

    async def movements(
        self,
        session: AsyncSession,
        usuario_id: int,
        *,
        desde: datetime | None,
        ate: datetime | None,
        casa_id: int | None,
        titular_id: int | None,
        conta_casa_id: int | None,
    ) -> list[dict[str, int | None]]:
        movement = models.Movimento
        account = models.ContaCasa
        stmt = (
            select(
                movement.conta_casa_id.label("conta_casa_id"),
                *[
                    func.coalesce(
                        func.sum(case((movement.tipo == kind, movement.valor_centavos), else_=0)),
                        0,
                    ).label(label)
                    for kind, label in (
                        ("DEPOSITO", "depositos_centavos"),
                        ("SAQUE", "saques_centavos"),
                        ("BONUS", "bonus_centavos"),
                        ("TRANSFERENCIA", "transferencias_centavos"),
                        ("AJUSTE", "ajustes_centavos"),
                    )
                ],
            )
            .outerjoin(
                account,
                (account.id == movement.conta_casa_id)
                & (account.usuario_id == movement.usuario_id),
            )
            .where(movement.usuario_id == usuario_id)
            .group_by(movement.conta_casa_id)
        )
        if desde is not None:
            stmt = stmt.where(movement.ocorrido_em >= desde)
        if ate is not None:
            stmt = stmt.where(movement.ocorrido_em < ate)
        if casa_id is not None:
            stmt = stmt.where(account.casa_id == casa_id)
        if titular_id is not None:
            stmt = stmt.where(account.titular_id == titular_id)
        if conta_casa_id is not None:
            stmt = stmt.where(movement.conta_casa_id == conta_casa_id)
        return [dict(row) for row in (await session.execute(stmt)).mappings()]
