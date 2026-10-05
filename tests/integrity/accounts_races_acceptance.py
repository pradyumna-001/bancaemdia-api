"""Account clocks and races exercise shared domain paths, never fabricated exact rows."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_consolidacao import GAME, PLACEMENT, intake_telegram, owner, telegram_payload

from bancaemdia import models
from bancaemdia.domain.account_attribution import AccountUsage, ResolutionStatus, resolve_account
from bancaemdia.domain.consolidacao_aposta import ConsolidacaoRecusadaError, consolidate
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

from .collection_acceptance import (
    deliver,
    invariant,
    item_for,
    money,
    replay_equal,
)

pytestmark = pytest.mark.xdist_group("postgres")


@pytest.mark.parametrize("reference", ["default", "actual"])
async def test_optional_account_reference_game_day_default_and_historical_actual(
    api, request, reference
):
    boundary = PLACEMENT + timedelta(days=1)
    async with AsyncSession(api.engine) as session, session.begin():
        await owner(session, api.user)
        first = await session.get(models.ContaCasa, api.account)
        second = models.ContaCasa(
            usuario_id=api.user, casa_id=first.casa_id, apelido="Game default", desde=boundary
        )
        session.add(second)
        await session.flush()
        later = second.id
        first.ate, first.ativa = boundary, False
        repository = UsoContaCasaRepo()
        usage = await repository.open(
            session, api.user, first.casa_id, first.id, PLACEMENT - timedelta(days=1)
        )
        await repository.close(session, usage.id, boundary)
        await repository.open(session, api.user, first.casa_id, later, boundary)
    ticket = uuid4().hex
    item = item_for(api, ticket, reference=reference == "actual")
    assert await api.run(await deliver(api, item)) == "materialized"
    await intake_telegram(api.engine, api.user, telegram_payload(ticket))
    before = await invariant(api, request, money())
    expected = api.account if reference == "actual" else later
    assert before["relations"][0][2] == expected
    assert next(b for b in before["bets"] if b[1] == "casa")[5] == expected
    async with AsyncSession(api.engine) as session:
        await owner(session, api.user)
        event = await session.scalar(
            select(models.Evento.payload_json).where(
                models.Evento.usuario_id == api.user,
                models.Evento.tipo == "APOSTA_CRIADA",
                models.Evento.fonte == "casa",
            )
        )
        assert event is not None
    await replay_equal(api, request, money())


@pytest.mark.parametrize("identities", [1, 2])
async def test_absent_reference_without_valid_game_usage_never_chooses_first(
    api, request, identities
):
    if identities == 2:
        async with api.admin.begin() as connection:
            house = await connection.scalar(
                select(models.ContaCasa.casa_id).where(models.ContaCasa.id == api.account)
            )
            await connection.execute(
                models.ContaCasa.__table__.insert().values(
                    usuario_id=api.user,
                    casa_id=house,
                    apelido="Ambiguity trap",
                    desde=PLACEMENT,
                )
            )
    assert (
        await api.run(await deliver(api, item_for(api, uuid4().hex, reference=False)))
        == "needs_review"
    )
    await replay_equal(api, request, money(count=0, stake=0, exposure=0), sources=0, lineages=0)


async def test_database_refuses_overlapping_default_usages_and_resolver_refuses_ambiguous_input(
    api, request
):
    async with AsyncSession(api.engine) as session, session.begin():
        await owner(session, api.user)
        first = await session.get(models.ContaCasa, api.account)
        second = models.ContaCasa(
            usuario_id=api.user, casa_id=first.casa_id, apelido="Other identity"
        )
        session.add(second)
        await session.flush()
        other = second.id
        house = first.casa_id
        repository = UsoContaCasaRepo()
        usage = await repository.open(session, api.user, first.casa_id, first.id, PLACEMENT)
        await repository.close(session, usage.id, GAME + timedelta(days=2))
    with pytest.raises(IntegrityError):
        async with AsyncSession(api.engine) as session, session.begin():
            await owner(session, api.user)
            await UsoContaCasaRepo().open(session, api.user, house, other, GAME)
    assert (
        resolve_account(
            GAME, [AccountUsage(api.account, PLACEMENT, None), AccountUsage(other, PLACEMENT, None)]
        ).status
        == ResolutionStatus.AMBIGUOUS
    )
    assert (
        resolve_account(GAME, [AccountUsage(api.account, None, GAME)]).status
        == ResolutionStatus.NONE
    )
    assert (
        resolve_account(GAME, [AccountUsage(api.account, GAME, None)]).conta_casa_id == api.account
    )
    await invariant(api, request, money(count=0, stake=0, exposure=0), sources=0, lineages=0)


@pytest.mark.parametrize("side", ["house", "telegram"])
async def test_competing_sources_stay_reviewed_and_two_workers_cannot_claim_same_endpoint(
    game_account, request, side
):
    api = game_account
    ticket = uuid4().hex
    house_tickets = [ticket, uuid4().hex] if side == "house" else [ticket]
    houses, tips = [], []
    for house_ticket in house_tickets:
        await api.run(await deliver(api, item_for(api, house_ticket)))
    for _ in range(1 if side == "house" else 2):
        tips.append(
            await intake_telegram(api.engine, api.user, telegram_payload(ticket, identity=False))
        )
    async with AsyncSession(api.engine) as session:
        await owner(session, api.user)
        houses = list(
            await session.scalars(
                select(models.Aposta.id)
                .where(models.Aposta.usuario_id == api.user, models.Aposta.origem == "casa")
                .order_by(models.Aposta.id)
            )
        )
        reviews = list(
            await session.scalars(
                select(models.RevisaoPendente.id).where(
                    models.RevisaoPendente.usuario_id == api.user,
                    models.RevisaoPendente.resolvido_em.is_(None),
                )
            )
        )
        assert reviews
    before = await invariant(
        api, request, money(count=3, stake=30000, exposure=30000), sources=3, lineages=len(houses)
    )
    assert before["relations"] == []

    async def reviewed(house, tip):
        try:
            async with AsyncSession(api.engine) as session, session.begin():
                await owner(session, api.user)
                relation = await consolidate(
                    session, api.user, house, tip, decision="reviewed", actor_id=api.user
                )
                return relation.id
        except ConsolidacaoRecusadaError:
            return None

    pairs = [(h, t) for h in houses for t in tips]
    result = await asyncio.wait_for(asyncio.gather(*(reviewed(h, t) for h, t in pairs)), 30)
    assert sum(r is not None for r in result) == 1
    await replay_equal(
        api, request, money(count=2, stake=20000, exposure=20000), sources=3, lineages=len(houses)
    )


async def test_two_review_workers_same_pair_commit_one_relation_and_two_audit_events(
    game_account, request
):
    api = game_account
    ticket = uuid4().hex
    await api.run(await deliver(api, item_for(api, ticket)))
    tip = await intake_telegram(api.engine, api.user, telegram_payload(ticket, identity=False))
    async with AsyncSession(api.engine) as session:
        await owner(session, api.user)
        house = await session.scalar(
            select(models.Aposta.id).where(
                models.Aposta.usuario_id == api.user, models.Aposta.origem == "casa"
            )
        )

    async def review():
        async with AsyncSession(api.engine) as session, session.begin():
            await owner(session, api.user)
            relation = await consolidate(
                session, api.user, house, tip, decision="reviewed", actor_id=api.user
            )
            return relation.id

    result = await asyncio.wait_for(asyncio.gather(*(review() for _ in range(10))), 30)
    assert len(set(result)) == 1
    await replay_equal(api, request, money())
