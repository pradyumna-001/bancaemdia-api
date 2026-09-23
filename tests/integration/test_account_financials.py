"""Account/holder/user centavo reconciliation against the canonical bet view."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1 import titulares as api
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo
from bancaemdia.repositories.titular_repo import TitularRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]
Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]


async def test_financials_reconcile_and_cash_remains_separate(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    user_id, other_id = await novo_usuario(), await novo_usuario()
    async with engine_admin.begin() as conn:
        house_id = await conn.scalar(
            insert(models.Casa)
            .values(nome=f"Financials {uuid4().hex[:12]}")
            .returning(models.Casa.id)
        )
    assert house_id is not None
    first = datetime(2026, 9, 10, 12, tzinfo=UTC)
    later = datetime(2026, 9, 11, 12, tzinfo=UTC)
    async with como(engine_app, user_id) as session:
        holder = await TitularRepo().create(session, user_id, "Ana")
        other_holder = await TitularRepo().create(session, user_id, "Bia")
        account1 = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": user_id,
                "titular_id": holder.id,
                "casa_id": house_id,
                "apelido": "ana",
            },
        )
        account2 = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": user_id,
                "titular_id": other_holder.id,
                "casa_id": house_id,
                "apelido": "bia",
            },
        )
        bets = [
            (account1.id, first, "GREEN", 10_000, 10_000, 18_000, False),
            (account1.id, later, "PENDENTE", 7_000, 7_000, None, False),
            (account2.id, later, "RED", 4_000, 4_000, 0, False),
            (None, first, "GREEN", 0, 5_000, 5_000, True),
            (None, later, "RED", 2_000, 2_000, 0, False),
        ]
        for account_id, occurred, state, stake, face, payout, freebet in bets:
            await session.execute(
                insert(models.Aposta).values(
                    usuario_id=user_id,
                    origem="manual",
                    stake_unidades=1.0,
                    conta_casa_id=account_id,
                    data_aposta=occurred,
                    estado=state,
                    stake_centavos=stake,
                    valor_aposta_centavos=face,
                    retorno_centavos=payout,
                    freebet=freebet,
                )
            )
        for account_id, kind, value in (
            (account1.id, "DEPOSITO", 50_000),
            (account1.id, "SAQUE", -1_000),
            (account2.id, "DEPOSITO", 10_000),
            (account2.id, "BONUS", 500),
        ):
            await session.execute(
                insert(models.Movimento).values(
                    usuario_id=user_id,
                    conta_casa_id=account_id,
                    tipo=kind,
                    valor_centavos=value,
                    ocorrido_em=first,
                )
            )
        await session.commit()
    async with como(engine_app, user_id) as session:
        user = await UsuarioRepo().get_by_id(session, user_id)
        assert user is not None
        result = await api.consultar_financeiro(user, session)
        assert result.total["lucro_centavos"] == 7_000
        assert result.total["giro_centavos"] == 16_000
        assert result.total["base_roi_centavos"] == 21_000
        assert result.total["exposicao_aberta_centavos"] == 7_000
        assert result.total["apostas"] == 5
        assert result.nao_atribuidas["lucro_centavos"] == 3_000
        assert result.total["depositos_centavos"] == 60_000
        assert result.total["saques_centavos"] == -1_000
        assert result.total["bonus_centavos"] == 500
        assert (
            sum(row.metricas["lucro_centavos"] for row in result.titulares)
            + result.nao_atribuidas["lucro_centavos"]
            == result.total["lucro_centavos"]
        )
        assert result.saldo_atual_centavos is not None
        assert result.saldo_atual_centavos != result.total["lucro_centavos"]
        only_first = await api.consultar_financeiro(user, session, ate=later)
        assert only_first.total["lucro_centavos"] == 13_000
        assert only_first.total["apostas"] == 2
        only_account = await api.consultar_financeiro(user, session, conta_casa_id=account1.id)
        assert only_account.total["lucro_centavos"] == 8_000
        assert only_account.nao_atribuidas["apostas"] == 0
        only_holder = await api.consultar_financeiro(user, session, titular_id=holder.id)
        assert only_holder.total["lucro_centavos"] == 8_000
        holder_bets, bet_count = await ApostaRepo().list_page(
            session, user_id, {"titular_id": holder.id}
        )
        assert bet_count == 2 and {bet.conta_casa_id for bet in holder_bets} == {account1.id}
        account_bets, account_count = await ApostaRepo().list_page(
            session, user_id, {"conta_casa_id": account2.id}
        )
        assert account_count == 1 and account_bets[0].conta_casa_id == account2.id
        holder_movements, movement_count = await MovimentoRepo().list_page(
            session, user_id, {"titular_id": holder.id}
        )
        assert movement_count == 2
        assert {movement.conta_casa_id for movement in holder_movements} == {account1.id}
    async with engine_admin.connect() as conn:
        canonical = await conn.execute(
            text(
                "SELECT COALESCE(SUM(lucro_centavos), 0) "
                "FROM painel.apostas_metricas WHERE usuario_id = :uid"
            ),
            {"uid": user_id},
        )
        assert canonical.scalar_one() == result.total["lucro_centavos"]
    async with como(engine_app, other_id) as session:
        other = await UsuarioRepo().get_by_id(session, other_id)
        assert other is not None
        foreign = await api.consultar_financeiro(other, session)
        assert foreign.total["apostas"] == 0
        assert foreign.titulares == []
