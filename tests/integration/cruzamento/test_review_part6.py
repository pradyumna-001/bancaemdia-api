"""Telegram confirmation, canonical analytics and goals share one financial fact."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from test_consolidacao import (
    GAME,
    PLACEMENT,
    accounts,
    financial,
    house_payload,
    intake_house,
    owner,
)

from bancaemdia import models
from bancaemdia.api.v1.metas import _progresso
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.domain.consolidacao_aposta import consolidate
from bancaemdia.domain.painel import FiltrosPainel
from bancaemdia.repositories.analytics_repo import AnalyticsRepo
from bancaemdia.services.telegram_confirmation import confirm_draft
from bancaemdia.services.telegram_conversation import open_draft
from bancaemdia.workers.telegram_materialization import draft_bet_key

pytestmark = pytest.mark.xdist_group("postgres")


async def test_confirmed_bot_and_house_count_once_in_analytics_goals_and_replay(
    engine_admin, engine_app, novo_usuario
):
    user = await novo_usuario()
    house, ids = await accounts(engine_admin, engine_app, user)
    chat = uuid4().int & ((1 << 62) - 1)
    async with AsyncSession(engine_app, expire_on_commit=False) as session, session.begin():
        await owner(session, user)
        session.add(
            models.TelegramLink(usuario_id=user, telegram_user_id=chat, telegram_chat_id=chat)
        )
        draft, _ = await open_draft(
            session,
            user_id=user,
            chat_id=chat,
            message_id=1,
            update_id=chat,
            extracted={
                "casa": "Betano",
                "odd": 2.0,
                "stake_unidades": 1.0,
                "data_aposta": PLACEMENT.isoformat(),
                "data_jogo": GAME.isoformat(),
            },
        )
        reply = await confirm_draft(session, user_id=user, chat_id=chat, update_id=chat + 1)
        assert reply.queued
        bot = await session.scalar(
            select(models.Aposta).where(models.Aposta.chave == draft_bet_key(draft.id))
        )
        assert bot.origem == "telegram_bot" and bot.conta_casa_id == ids[0]
        bot_id = bot.id
    casa = await intake_house(engine_app, user, house, house_payload(uuid4().hex, state="Win"))
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        relation = await consolidate(
            session, user, casa, bot_id, decision="reviewed", actor_id=user
        )
        assert relation.conta_casa_id == ids[0]
        meta = models.MetaDesempenho(
            usuario_id=user,
            titulo="Synthetic",
            metrica="total_apostas",
            inicio=date(2026, 9, 1),
            fim=date(2026, 9, 30),
            alvo=Decimal(1),
            linha_base=Decimal(0),
        )
        session.add(meta)
        await session.flush()
        meta_id = meta.id
    for replay in (False, True):
        if replay:
            await reconstruir_usuario(user, engine=engine_app)
        totals, relations = await financial(engine_app, user)
        assert totals == {"count": 1, "stake": 10000, "return": 20000, "exposure": 0}
        assert len(relations) == 1 and relations[0].estado == "active"
        async with AsyncSession(engine_app) as session:
            await owner(session, user)
            analytics = await AnalyticsRepo().consultar(
                session,
                user,
                FiltrosPainel.criar("7d", agora=GAME + timedelta(days=1)),
                "America/Sao_Paulo",
            )
            assert analytics["total_filtrado"]["total_apostas"] == 1
            assert analytics["total_filtrado"]["lucro_centavos"] == 10000
            goal = await session.get(models.MetaDesempenho, meta_id)
            progress = await _progresso(session, goal)
            assert Decimal(progress.valor_atual) == 1 and progress.alvo_atingido
    async with engine_admin.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("SELECT billing_activate_rollout()"))
            # These rows were created by confirmation and actual collection/matching.
            for table in ("aposta_consolidacoes", "cruzamento_entradas"):
                assert await conn.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE usuario_id=:u"), {"u": user}
                )
                savepoint = await conn.begin_nested()
                with pytest.raises(DBAPIError) as exc:
                    await conn.execute(
                        text(f"UPDATE {table} SET usuario_id=usuario_id WHERE usuario_id=:u"),
                        {"u": user},
                    )
                assert exc.value.orig.sqlstate == "P0402"
                await savepoint.rollback()
        finally:
            await transaction.rollback()
