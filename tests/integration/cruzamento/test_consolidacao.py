"""Synthetic product acceptance on PostgreSQL, using actual intake and workers."""

import asyncio
import copy
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.v1.coleta import registrar
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.domain.account_attribution import ResolutionStatus
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
)
from bancaemdia.domain.consolidacao_aposta import ConsolidacaoRecusadaError, consolidate, unlink
from bancaemdia.repositories.aposta_consolidacao import financial_predicate
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.extrato_repo import ExtratoRepo
from bancaemdia.workers.materialization import ExtracaoDoJson, gravar_coletas, gravar_leitura

pytestmark = pytest.mark.xdist_group("postgres")
PLACEMENT = datetime(2026, 9, 20, 12, tzinfo=UTC)
GAME = datetime(2026, 9, 22, 22, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def require_postgres():
    if not os.environ.get("TEST_DATABASE_URL"):
        from testcontainers.core.docker_client import DockerClient

        assert DockerClient().client.ping(), "Consolidation acceptance requires real PostgreSQL"


async def owner(session, user):
    await session.execute(
        text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(user)}
    )


async def accounts(engine_admin, engine_app, user, *, count=1):
    async with engine_admin.begin() as conn:
        await conn.execute(
            insert(models.Casa)
            .values(nome="Betano", dominio="betano.bet.br")
            .on_conflict_do_nothing()
        )
        house = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    async with AsyncSession(engine_app, expire_on_commit=False) as session, session.begin():
        await owner(session, user)
        rows = []
        for index in range(count):
            account = models.ContaCasa(
                usuario_id=user,
                casa_id=house,
                apelido=f"synthetic-{index}",
                desde=PLACEMENT - timedelta(days=1),
            )
            session.add(account)
            await session.flush()
            rows.append(account.id)
        return house, rows


def house_payload(ticket, *, state=None, return_value=200):
    payload = {
        "id": ticket,
        "totalAmount": 100,
        "totalOdds": 2.0,
        "placedAt": int(PLACEMENT.timestamp() * 1000),
        "bonusType": 0,
        "totalAmountWithCurrency": {"currencyCode": "BRL"},
        "legs": [
            {
                "legItems": [
                    {
                        "eventId": "synthetic-42",
                        "eventName": "Azul - Verde",
                        "startTime": int(GAME.timestamp() * 1000),
                        "selections": [
                            {
                                "description": "Mais de 2.5",
                                "market": "Total de gols",
                                "handicap": 2.5,
                                "odds": 2.0,
                            }
                        ],
                    }
                ]
            }
        ],
    }
    if state:
        payload.update(
            finalBetResult=state,
            finalWinnings=return_value,
            settledAt=int((GAME + timedelta(hours=2)).timestamp() * 1000),
        )
    return payload


def telegram_payload(ticket, *, identity=True, chat=None, message=None):
    return {
        "chat_id": chat or uuid4().int % (2**60),
        "message_id": message or uuid4().int % (2**60),
        "postada_em": PLACEMENT.isoformat(),
        "bilhete": {
            "casa": "Betano",
            "tipo": "simples",
            "evento": "Azul - Verde",
            "odd_total": 2.0,
            "quando": GAME.isoformat(),
            "confianca": 0.99,
            "identidade_bilhete": ticket if identity else None,
            "ocorrido_em": PLACEMENT.isoformat(),
            "stake_unidades": 1.0,
            "selecoes": [
                {
                    "evento": "Azul - Verde",
                    "mercado": "Total de gols",
                    "escolha": "Mais de 2.5",
                    "linha": 2.5,
                    "odd": 2.0,
                }
            ],
        },
    }


async def intake_house(engine, user, house_id, payload):
    async with AsyncSession(engine) as session, session.begin():
        await owner(session, user)
        result, jobs = await registrar(session, user, "betano", house_id, [payload])
        assert result.recusadas == []
    await gravar_coletas(engine, user, jobs)
    async with AsyncSession(engine) as session:
        await owner(session, user)
        return await session.scalar(
            select(models.Aposta.id).where(
                models.Aposta.usuario_id == user, models.Aposta.chave == f"c:betano:{payload['id']}"
            )
        )


async def intake_telegram(engine, user, payload):
    await gravar_leitura(
        engine, user, ExtracaoDoJson.model_validate(payload), payload, "synthetic-media"
    )
    async with AsyncSession(engine) as session:
        await owner(session, user)
        return await session.scalar(
            select(models.Aposta.id).where(
                models.Aposta.usuario_id == user,
                models.Aposta.chat_id == payload["chat_id"],
                models.Aposta.message_id == payload["message_id"],
            )
        )


async def financial(engine, user):
    async with AsyncSession(engine) as session:
        await owner(session, user)
        bets = list(
            await session.scalars(
                select(models.Aposta).where(
                    models.Aposta.usuario_id == user,
                    models.Aposta.selecionada,
                    financial_predicate(),
                )
            )
        )
        relations = list(
            await session.scalars(
                select(models.ApostaConsolidacao).where(
                    models.ApostaConsolidacao.usuario_id == user
                )
            )
        )
        return {
            "count": len(bets),
            "stake": sum(b.stake_centavos for b in bets),
            "return": sum(b.retorno_centavos or 0 for b in bets),
            "exposure": sum(b.stake_centavos for b in bets if b.estado == "PENDENTE"),
        }, relations


@pytest.mark.parametrize("order", ["house-first", "telegram-first"])
async def test_real_intake_both_orders_one_fact_settlement_and_source_preservation(
    engine_app, engine_admin, novo_usuario, order
):
    user = await novo_usuario()
    house_id, account_ids = await accounts(engine_admin, engine_app, user)
    ticket = "synthetic-" + uuid4().hex
    raw, tg = house_payload(ticket), telegram_payload(ticket)
    if order == "house-first":
        house = await intake_house(engine_app, user, house_id, raw)
        tip = await intake_telegram(engine_app, user, tg)
    else:
        tip = await intake_telegram(engine_app, user, tg)
        house = await intake_house(engine_app, user, house_id, raw)
    totals, relations = await financial(engine_app, user)
    assert totals == {"count": 1, "stake": 10000, "return": 0, "exposure": 10000}
    assert len(relations) == 1 and relations[0].estado == "active"
    assert relations[0].casa_aposta_id == house and relations[0].telegram_aposta_id == tip
    assert relations[0].conta_casa_id == account_ids[0]
    assert relations[0].contexto["midia_hash"] == "synthetic-media"
    assert relations[0].evidencia["veredicto"]["classification"] == "exact"
    await intake_house(engine_app, user, house_id, house_payload(ticket, state="Win"))
    # Telegram rereading may contradict money; it cannot overwrite the Casa fact or re-enable it.
    updated_tg = copy.deepcopy(tg)
    updated_tg["bilhete"]["odd_total"] = 3.0
    await intake_telegram(engine_app, user, updated_tg)
    totals, repeated = await financial(engine_app, user)
    assert totals == {"count": 1, "stake": 10000, "return": 20000, "exposure": 0}
    assert repeated[0].id == relations[0].id
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        rows = list(
            await session.scalars(select(models.Aposta).where(models.Aposta.usuario_id == user))
        )
        assert len(rows) == 2 and all(b.selecionada for b in rows)
        assert next(b for b in rows if b.id == house).odd == pytest.approx(2.0)
        assert next(b for b in rows if b.id == tip).odd == pytest.approx(3.0)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ColetaCasa)
                .where(models.ColetaCasa.usuario_id == user)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(
                    models.Evento.usuario_id == user, models.Evento.tipo == "APOSTAS_CONSOLIDADAS"
                )
            )
            == 2
        )
        ledger, total = await ExtratoRepo().list_page(session, user, {})
        assert total == 1 and ledger[0].resultado_liquido_centavos == 10000
        listed, count = await ApostaRepo().list_page(session, user, {})
        assert count == 1 and listed[0].id == house
    result = await reconstruir_usuario(user, engine=engine_app)
    assert result.apostas == 2
    assert (await financial(engine_app, user))[0] == totals


async def test_default_account_follows_game_day_while_explicit_multiaccount_keeps_actual_account(
    engine_app, engine_admin, novo_usuario
):
    user = await novo_usuario()
    house, ids = await accounts(engine_admin, engine_app, user, count=2)
    switch = PLACEMENT + timedelta(days=1)
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await session.execute(
            update(models.ContaCasa)
            .where(models.ContaCasa.id == ids[0])
            .values(ate=switch, ativa=False)
        )
        await session.execute(
            update(models.ContaCasa).where(models.ContaCasa.id == ids[1]).values(desde=switch)
        )
        default = await attribute_account(session, user, "Betano", GAME)
        actual = await attribute_account(session, user, "Betano", GAME, ids[0])
        assert default.conta_casa_id == ids[1]
        assert actual.conta_casa_id == ids[0]
        assert (await attribute_account(session, user, "Betano", switch)).conta_casa_id == ids[1]
        assert (
            await attribute_account(session, user, "Betano", None)
        ).status == ResolutionStatus.NONE
    ticket = "synthetic-" + uuid4().hex
    house_bet = await intake_house(engine_app, user, house, house_payload(ticket))
    await intake_telegram(engine_app, user, telegram_payload(ticket))
    totals, relations = await financial(engine_app, user)
    assert totals["count"] == 1 and relations[0].conta_casa_id == ids[1]
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        assert (await session.get(models.Aposta, house_bet)).conta_casa_id == ids[1]
    await reconstruir_usuario(user, engine=engine_app)
    assert (await financial(engine_app, user))[1][0].conta_casa_id == ids[1]


@pytest.mark.parametrize("count", [0, 2])
async def test_missing_or_ambiguous_account_never_consolidates_or_chooses_first(
    engine_app, engine_admin, novo_usuario, count
):
    user = await novo_usuario()
    house, _ = await accounts(engine_admin, engine_app, user, count=count)
    ticket = "synthetic-" + uuid4().hex
    await intake_house(engine_app, user, house, house_payload(ticket))
    await intake_telegram(engine_app, user, telegram_payload(ticket))
    totals, relations = await financial(engine_app, user)
    assert totals["stake"] == 20000 and relations == []
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        assert all(
            b.conta_casa_id is None
            for b in await session.scalars(
                select(models.Aposta).where(models.Aposta.usuario_id == user)
            )
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.RevisaoPendente)
                .where(
                    models.RevisaoPendente.usuario_id == user,
                    models.RevisaoPendente.resolvido_em.is_(None),
                )
            )
            >= 1
        )


async def test_probable_missing_identity_preserves_two_facts_until_explicit_review(
    engine_app, engine_admin, novo_usuario
):
    user = await novo_usuario()
    house, _ = await accounts(engine_admin, engine_app, user)
    ticket = "synthetic-" + uuid4().hex
    house_bet = await intake_house(engine_app, user, house, house_payload(ticket))
    tip = await intake_telegram(engine_app, user, telegram_payload(ticket, identity=False))
    assert (await financial(engine_app, user))[0]["count"] == 2
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        relation = await consolidate(
            session, user, house_bet, tip, decision="reviewed", actor_id=user
        )
        ident = relation.id
    assert (await financial(engine_app, user))[0]["count"] == 1
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await unlink(session, user, ident, actor_id=user, reason="synthetic reclassification")
    assert (await financial(engine_app, user))[0]["count"] == 2
    with pytest.raises(ConsolidacaoRecusadaError, match="revisada"):
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            await consolidate(session, user, house_bet, tip)
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        newer = await consolidate(session, user, house_bet, tip, decision="reviewed", actor_id=user)
        assert newer.id != ident
    _, relations = await financial(engine_app, user)
    assert [r.estado for r in relations] == ["unlinked", "active"]


async def test_concurrent_same_pair_retry_and_transaction_rollback(
    engine_app, engine_admin, novo_usuario
):
    user = await novo_usuario()
    house, _ = await accounts(engine_admin, engine_app, user)
    ticket = "synthetic-" + uuid4().hex
    house_bet = await intake_house(engine_app, user, house, house_payload(ticket))
    tip = await intake_telegram(engine_app, user, telegram_payload(ticket, identity=False))
    with pytest.raises(RuntimeError, match="injected"):
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            await consolidate(session, user, house_bet, tip, decision="reviewed", actor_id=user)
            raise RuntimeError("injected rollback after relation/events/source update")
    assert (await financial(engine_app, user))[1] == []

    async def apply():
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            relation = await consolidate(
                session, user, house_bet, tip, decision="reviewed", actor_id=user
            )
            return relation.id

    ids = await asyncio.wait_for(asyncio.gather(*(apply() for _ in range(10))), timeout=30)
    assert len(set(ids)) == 1
    assert (await financial(engine_app, user))[0]["stake"] == 10000
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(
                    models.Evento.usuario_id == user, models.Evento.tipo == "APOSTAS_CONSOLIDADAS"
                )
            )
            == 2
        )


async def test_cross_tenant_rls_fk_and_explicit_reference_fail_closed(
    engine_app, engine_admin, novo_usuario
):
    user, foreign = await novo_usuario(), await novo_usuario()
    house, ids = await accounts(engine_admin, engine_app, user)
    _, foreign_ids = await accounts(engine_admin, engine_app, foreign)
    ticket = "synthetic-" + uuid4().hex
    casa = await intake_house(engine_app, user, house, house_payload(ticket))
    tip = await intake_telegram(engine_app, user, telegram_payload(ticket))
    foreign_tip = await intake_telegram(engine_app, foreign, telegram_payload(ticket))
    relation = (await financial(engine_app, user))[1][0]
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, foreign)
        assert await session.get(models.ApostaConsolidacao, relation.id) is None
        assert (
            await session.execute(
                update(models.ApostaConsolidacao)
                .where(models.ApostaConsolidacao.id == relation.id)
                .values(estado="unlinked", desvinculada_em=GAME)
            )
        ).rowcount == 0
    with pytest.raises(InvalidAccountReferenceError):
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            await attribute_account(session, user, "Betano", GAME, foreign_ids[0])
    with pytest.raises(DBAPIError):
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            session.add(
                models.ApostaConsolidacao(
                    usuario_id=user,
                    casa_aposta_id=casa,
                    telegram_aposta_id=foreign_tip,
                    conta_casa_id=ids[0],
                    estado="active",
                    decisao="reviewed",
                    versao="synthetic",
                    evidencia={},
                    contexto={},
                    ator="synthetic",
                )
            )
            await session.flush()
    with pytest.raises(ConsolidacaoRecusadaError):
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, foreign)
            await consolidate(session, foreign, casa, tip, decision="reviewed", actor_id=foreign)
