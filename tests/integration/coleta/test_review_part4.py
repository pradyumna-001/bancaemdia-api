"""Game-time attribution through real v2 HTTP, durable inbox and restricted PostgreSQL."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_contract_v2 import api as api
from test_contract_v2 import capture, digest
from test_pairing import real_database_required as real_database_required
from test_pairing import signing_key as signing_key
from test_pairing import system as system

from bancaemdia import models
from bancaemdia.repositories.coleta_instalacao import owner_scope
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo

pytestmark = pytest.mark.xdist_group("postgres")


@pytest.fixture
async def engine_admin(banco_migracao):
    # Rollout activation is global: isolate it from other xdist workers' tenants.
    engine = create_async_engine(banco_migracao.url_admin)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def engine_app(banco_migracao):
    engine = create_async_engine(banco_migracao.url_app)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("game_available", [False, True])
async def test_v2_game_clock_and_actual_multiconta_are_distinct(api, explicit, game_available):
    item = capture(api.account if explicit else None)
    raw = item["payload"]
    switch = datetime(2026, 8, 2, 22, 15, tzinfo=UTC)
    game = datetime(2026, 8, 3, 22, 30, tzinfo=UTC)
    async with api.admin.begin() as conn:
        house = await conn.scalar(
            select(models.ContaCasa.casa_id).where(models.ContaCasa.id == api.account)
        )
        new = await conn.scalar(
            models.ContaCasa.__table__
            .insert()
            .values(usuario_id=api.user, casa_id=house, apelido="Game account")
            .returning(models.ContaCasa.id)
        )
        await conn.execute(
            models.UsoContaCasa.__table__.insert(),
            [
                {
                    "usuario_id": api.user,
                    "casa_id": house,
                    "conta_casa_id": api.account,
                    "vigente_de": datetime(2026, 8, 1, tzinfo=UTC),
                    "vigente_ate": switch,
                    "origem": "EXPLICITA",
                },
                {
                    "usuario_id": api.user,
                    "casa_id": house,
                    "conta_casa_id": new,
                    "vigente_de": switch,
                    "vigente_ate": None,
                    "origem": "EXPLICITA",
                },
            ],
        )
    for leg in raw["legs"]:
        for selection in leg["legItems"]:
            if game_available:
                selection["startTime"] = int(game.timestamp() * 1000)
            else:
                selection.pop("startTime", None)
    item["content_hash"] = digest(raw)
    async with AsyncSession(api.engine) as session:
        await owner_scope(session, api.user)
        legacy = await ContaCasaRepo().get_vigente_by_nome_da_casa(
            session, api.user, "Betano", game if game_available else None
        )
        assert (None if legacy is None else legacy.id) == (new if game_available else None)
    ack = (await api.send([item])).json()["items"][0]
    status = "materialized" if explicit or game_available else "needs_review"
    assert await api.run(ack) == status
    assert await api.run(ack) == status
    assert (await api.send([item])).json()["items"] == [ack]
    async with api.admin.connect() as conn:
        bet = (
            (
                await conn.execute(
                    select(models.Aposta.__table__).where(models.Aposta.usuario_id == api.user)
                )
            )
            .mappings()
            .one_or_none()
        )
        if status == "needs_review":
            assert bet is None
            return
        assert bet["conta_casa_id"] == (api.account if explicit else new)
        assert bet["data_jogo"] == (game if game_available else None)
        event = await conn.scalar(
            select(models.Evento.payload_json).where(
                models.Evento.usuario_id == api.user, models.Evento.tipo == "APOSTA_CRIADA"
            )
        )
        assert event["conta_referencia_explicita"] is explicit
        assert event["conta_casa_ref"] == (api.account if explicit else None)


async def test_expired_collection_access_pauses_ack_and_resumes_once_after_paid_grant(api):
    from uuid import UUID

    from bancaemdia.models.coleta_sessao import ColetaEntrega

    ack = (await api.send([capture(api.account)])).json()["items"][0]
    async with api.admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("SELECT billing_activate_rollout()"))
        await conn.execute(
            text(
                "UPDATE assinaturas SET status='EXPIRED', trial_confirmed=false WHERE usuario_id=:uid"
            ),
            {"uid": api.user},
        )
    try:
        # Pairing remains an identity control; fresh financial payloads require access.
        assert (await api.pair())["token"]
        denied = await api.send([capture(api.account)])
        assert denied.status_code == 402 and denied.json()["detail"] == "account_read_only"
        for _ in range(4):
            assert await api.run(ack) == "pending"
        async with api.admin.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(ColetaEntrega.__table__).where(
                            ColetaEntrega.job_id == UUID(ack["job_id"])
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert row["reason"] == "account_read_only" and row["tentativas"] == 0
            assert row["envelope"] and row["finalizada_em"] is None
            assert not await conn.scalar(
                select(models.Aposta.id).where(models.Aposta.usuario_id == api.user)
            )
            price = await conn.scalar(
                text(
                    "INSERT INTO billing_prices(amount_cents,currency,frequency,valid_from,valid_until,published) VALUES (1000,'BRL','MONTHLY',now(),now()+interval '2 hours',true) RETURNING id"
                )
            )
            await conn.execute(
                text(
                    "UPDATE assinaturas SET status='ACTIVE',price_id=:price,current_period_started_at=now()-interval '1 hour',current_period_ends_at=now()+interval '1 hour' WHERE usuario_id=:uid"
                ),
                {"price": price, "uid": api.user},
            )
        assert await api.run(ack) == "materialized"
        assert await api.run(ack) == "materialized"
        async with api.admin.connect() as conn:
            assert (
                len(
                    (
                        await conn.execute(
                            select(models.Aposta.id).where(models.Aposta.usuario_id == api.user)
                        )
                    ).all()
                )
                == 1
            )
    finally:
        # Only this disposable test database; preserve the prior rollout setting for later cases.
        async with api.admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:previous WHERE id=1"),
                {"previous": previous},
            )
