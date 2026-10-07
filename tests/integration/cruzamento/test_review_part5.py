"""Administrator review regressions on the actual integrated product."""

from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_consolidacao import (
    GAME,
    PLACEMENT,
    accounts,
    application,
    financial,
    house_payload,
    intake_house,
    intake_telegram,
    owner,
    telegram_payload,
)

from bancaemdia import models
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.domain.consolidacao_aposta import ConsolidacaoRecusadaError, consolidate
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

pytestmark = pytest.mark.xdist_group("postgres")


async def test_forged_exact_score_cannot_authorize_a_probable_pair(
    engine_app, engine_admin, novo_usuario
):
    user = await novo_usuario()
    house, _ = await accounts(engine_admin, engine_app, user)
    ticket = uuid4().hex
    casa = await intake_house(engine_app, user, house, house_payload(ticket))
    tip = await intake_telegram(engine_app, user, telegram_payload(ticket, identity=False))
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        pair = await session.scalar(
            select(models.CruzamentoCandidato).where(models.CruzamentoCandidato.usuario_id == user)
        )
        assert pair.status == "probable"
        await session.execute(
            update(models.CruzamentoCandidato)
            .where(models.CruzamentoCandidato.id == pair.id)
            .values(status="exact", score=100)
        )
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        with pytest.raises(ConsolidacaoRecusadaError, match="elegibilidade obsoleta"):
            await consolidate(session, user, casa, tip)
        await session.rollback()
    totals, relations = await financial(engine_app, user)
    assert totals["count"] == 2 and totals["stake"] == 20000 and not relations


async def test_game_correction_preserves_default_actual_and_explicit_clear_after_replay(
    engine_app, engine_admin, novo_usuario
):
    user = await novo_usuario()
    house, ids = await accounts(engine_admin, engine_app, user, count=2)
    switch = PLACEMENT + timedelta(days=1)
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        first = await UsoContaCasaRepo().open(
            session, user, house, ids[0], PLACEMENT - timedelta(days=1)
        )
        await UsoContaCasaRepo().close(session, first.id, switch)
        await UsoContaCasaRepo().open(session, user, house, ids[1], switch)
    keys = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(engine_app, user)), base_url="http://test"
    ) as client:
        body = {"casa": "Betano", "odd": 2, "stake_unidades": 1, "data_jogo": PLACEMENT.isoformat()}
        for actual in (None, ids[0]):
            created = await client.post("/api/v1/apostas", json={**body, "conta_casa_ref": actual})
            assert created.status_code == 201, created.text
            key = created.json()["aposta"]["chave"]
            keys.append(key)
            corrected = await client.patch(
                f"/api/v1/apostas/{key}", json={"data_jogo": GAME.isoformat()}
            )
            assert corrected.status_code == 200, corrected.text
            assert corrected.json()["aposta"]["conta_casa_id"] == (
                ids[1] if actual is None else ids[0]
            )
        cleared = await client.patch(f"/api/v1/apostas/{keys[1]}", json={"conta_casa_id": None})
        assert cleared.status_code == 200 and cleared.json()["aposta"]["conta_casa_id"] is None
    await reconstruir_usuario(user, engine=engine_app)
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        rows = list(
            await session.scalars(
                select(models.Aposta)
                .where(models.Aposta.usuario_id == user)
                .order_by(models.Aposta.id)
            )
        )
        assert [bet.conta_casa_id for bet in rows] == [ids[1], None]
