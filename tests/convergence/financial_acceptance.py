"""Explicitly selected CI acceptance of the disposable dependency composition."""

import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).parents[1] / "integration/coleta"))
sys.path.insert(0, str(Path(__file__).parents[1] / "integration/cruzamento"))
from test_consolidacao import (
    GAME,
    PLACEMENT,
    accounts,
    financial,
    house_payload,
    intake_house,
    intake_telegram,
    owner,
    telegram_payload,
)
from test_contract_v2 import api as api
from test_contract_v2 import capture, digest
from test_contract_v2 import real_database_required as real_database_required
from test_contract_v2 import signing_key as signing_key
from test_contract_v2 import system as system

from bancaemdia import models
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.domain.account_attribution_service import attribute_account
from bancaemdia.domain.titulares import TrocaPedido, trocar_conta
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

pytestmark = pytest.mark.xdist_group("postgres")


@pytest.mark.parametrize("order", ["house-first", "telegram-first"])
async def test_composed_temporal_switch_uses_game_and_consolidates_replays_both_orders(
    engine_app, engine_admin, novo_usuario, order
):
    user = await novo_usuario()
    house, ids = await accounts(engine_admin, engine_app, user, count=2)
    boundary = PLACEMENT + timedelta(days=1)
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await session.execute(
            update(models.ContaCasa).where(models.ContaCasa.id == ids[0]).values(estado="EM_USO")
        )
        await UsoContaCasaRepo().open(session, user, house, ids[0], PLACEMENT - timedelta(days=1))
    pedido = TrocaPedido(house, ids[0], ids[1], boundary, "LIMITADA")
    key = uuid4().hex
    for apply in (False, True):
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            await trocar_conta(session, user, pedido, key, aplicar=apply)
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        assert (await attribute_account(session, user, "Betano", PLACEMENT)).conta_casa_id == ids[0]
        assert (await attribute_account(session, user, "Betano", GAME)).conta_casa_id == ids[1]
        assert (
            await attribute_account(session, user, "Betano", GAME, ids[0])
        ).conta_casa_id == ids[0]
    ticket = uuid4().hex
    if order == "house-first":
        await intake_house(engine_app, user, house, house_payload(ticket))
        await intake_telegram(engine_app, user, telegram_payload(ticket))
    else:
        await intake_telegram(engine_app, user, telegram_payload(ticket))
        await intake_house(engine_app, user, house, house_payload(ticket))
    await intake_house(engine_app, user, house, house_payload(ticket, state="Win"))
    expected = {"count": 1, "stake": 10000, "return": 20000, "exposure": 0}
    totals, relations = await financial(engine_app, user)
    assert totals == expected and relations[0].conta_casa_id == ids[1]
    await reconstruir_usuario(user, engine=engine_app)
    assert (await financial(engine_app, user))[0] == expected


async def test_composed_v2_explicit_historical_account_wins_even_outside_placement_interval(api):
    # The explicit v2 reference identifies the account that really placed the bet.
    async with api.admin.begin() as conn:
        await conn.execute(
            update(models.ContaCasa)
            .where(models.ContaCasa.id == api.account)
            .values(ativa=False, ate=PLACEMENT - timedelta(days=2), estado="LIMITADA")
        )
    ticket = uuid4().hex
    item = capture(api.account, payload=house_payload(ticket), capturado_em="2026-09-25T12:00:00Z")
    item["content_hash"] = digest(item["payload"])
    response = await api.send([item])
    assert response.status_code == 200, response.text
    ack = response.json()["items"][0]
    assert await api.run(ack) == "materialized"
    await intake_telegram(api.engine, api.user, telegram_payload(ticket))
    totals, relations = await financial(api.engine, api.user)
    assert totals["count"] == 1 and totals["stake"] == 10000
    assert relations[0].conta_casa_id == api.account
    await reconstruir_usuario(api.user, engine=api.engine)
    assert (await financial(api.engine, api.user))[0] == totals
