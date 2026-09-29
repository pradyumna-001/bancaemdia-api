import asyncio
import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.models.cruzamento_candidato import CruzamentoCandidato as Pair
from bancaemdia.models.cruzamento_candidato import CruzamentoEntrada as Entry
from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo
from bancaemdia.services.cruzamento_candidatos import generate
from bancaemdia.workers.pairing import confirmar_par, parear_criacao

pytestmark = pytest.mark.xdist_group("postgres")
BASE = json.loads((Path(__file__).parents[1] / "fixtures/cruzamento/cases.json").read_text())[
    "base"
]


@pytest.fixture(scope="module", autouse=True)
def require_postgres():
    if not os.environ.get("TEST_DATABASE_URL"):
        from testcontainers.core.docker_client import DockerClient

        assert DockerClient().client.ping(), "Matching acceptance requires real PostgreSQL"


async def owner(session, user):
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user)}
    )


async def create(engine, user, origin, patch=None, *, run=True, key=None):
    state = {**copy.deepcopy(BASE), **(patch or {}), "usuario_id": user}
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        await owner(session, user)
        bet = models.Aposta(
            usuario_id=user,
            chave=key or f"{'c' if origin == 'casa' else 't'}:{uuid4().hex}",
            origem=origin,
            chat_id=1 if origin == "telegram" else None,
            message_id=uuid4().int % (2**60) if origin == "telegram" else None,
            stake_unidades=1,
            stake_centavos=state["stake_centavos"],
            valor_aposta_centavos=state["stake_centavos"],
            odd=state["odd"],
            estado=state["estado"],
            data_aposta=datetime(2026, 9, 20, 22, 30, tzinfo=UTC),
        )
        session.add(bet)
        await session.flush()
        session.add(
            models.Evento(
                usuario_id=user,
                aposta_chave=bet.chave,
                tipo="APOSTA_CRIADA",
                fonte="casa" if origin == "casa" else "ia",
                payload_json=state,
            )
        )
        await session.flush()
        verdict = await parear_criacao(session, user, bet, state) if run else "nova"
        return bet.id, state, verdict


async def refresh(engine, user, ident, state):
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        await owner(session, user)
        bet = await session.get(models.Aposta, ident)
        return await generate(session, user, bet, state)


async def pairs(engine, user):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await owner(session, user)
        return (
            await session.scalars(select(Pair).where(Pair.usuario_id == user).order_by(Pair.id))
        ).all()


async def test_exact_candidate_is_persisted_and_repeated_without_financial_mutation(
    engine_app, novo_usuario
):
    user = await novo_usuario()
    house, _, _ = await create(engine_app, user, "casa")
    tip, raw, verdict = await create(engine_app, user, "telegram")
    assert verdict == "igual"
    first = (await pairs(engine_app, user))[0]
    assert first.status == "exact" and first.score == 100
    assert first.evidencia["telegram"]["original"]["evento"] == "Azul - Verde"
    assert first.revisao_id is None
    await refresh(engine_app, user, tip, raw)
    assert len(await pairs(engine_app, user)) == 1
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        bets = (
            await session.scalars(select(models.Aposta).where(models.Aposta.id.in_([house, tip])))
        ).all()
        assert all(b.selecionada and b.parceira_chave is None for b in bets)
        assert sum(b.stake_centavos for b in bets) == 20000
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(models.Evento.usuario_id == user)
            )
            == 2
        )


@pytest.mark.parametrize("competing_origin", ["telegram", "casa"])
async def test_concurrent_competitors_demote_both_sides_and_create_one_review_each(
    engine_app, novo_usuario, competing_origin
):
    user = await novo_usuario()
    await create(engine_app, user, "casa" if competing_origin == "telegram" else "telegram")
    created = await asyncio.wait_for(
        asyncio.gather(
            create(engine_app, user, competing_origin),
            create(engine_app, user, competing_origin),
        ),
        timeout=30,
    )
    rows = await pairs(engine_app, user)
    assert len(rows) == 2 and all(p.status == "probable" for p in rows)
    assert all(p.revisao_id is not None for p in rows)
    await refresh(engine_app, user, created[0][0], created[0][1])
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.RevisaoPendente)
                .where(models.RevisaoPendente.usuario_id == user)
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == user, models.Aposta.selecionada)
            )
            == 3
        )


async def test_delayed_capture_uses_source_time_and_no_ingestion_fallback(engine_app, novo_usuario):
    user = await novo_usuario()
    await create(engine_app, user, "casa")
    tip, _, verdict = await create(
        engine_app, user, "telegram", {"capturado_em": "2040-01-01T00:00:00Z"}
    )
    assert verdict == "igual"
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await session.execute(
            update(models.Aposta)
            .where(models.Aposta.id == tip)
            .values(criada_em=datetime(2040, 1, 1, tzinfo=UTC))
        )
    _, _, verdict = await create(engine_app, user, "telegram", {"comeca_em": None})
    assert verdict == "nova"
    assert len(await pairs(engine_app, user)) == 1


async def test_incompatible_house_tenant_and_consolidated_bets_are_excluded(
    engine_app, novo_usuario
):
    user, other = await novo_usuario(), await novo_usuario()
    house, _, _ = await create(engine_app, user, "casa")
    await create(engine_app, other, "telegram")
    await create(engine_app, user, "telegram", {"casa": "Superbet"})
    assert await pairs(engine_app, user) == []
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await session.execute(
            update(models.Aposta).where(models.Aposta.id == house).values(parceira_chave="previous")
        )
    await create(engine_app, user, "telegram")
    assert await pairs(engine_app, user) == []


async def test_rls_and_composite_foreign_keys_block_cross_tenant_references(
    engine_app, novo_usuario
):
    user, other = await novo_usuario(), await novo_usuario()
    await create(engine_app, user, "casa")
    await create(engine_app, user, "telegram")
    foreign, _, _ = await create(engine_app, other, "telegram")
    row = (await pairs(engine_app, user))[0]
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, other)
        assert await session.get(Pair, row.id) is None
        assert (
            await session.execute(update(Pair).where(Pair.id == row.id).values(score=0))
        ).rowcount == 0
        assert await session.get(Entry, row.casa_aposta_id) is None
    with pytest.raises(DBAPIError):
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            await session.execute(
                update(Pair).where(Pair.id == row.id).values(telegram_aposta_id=foreign)
            )


async def test_financial_manual_confirmation_invalidates_candidates_and_reviews(
    engine_app, novo_usuario
):
    user = await novo_usuario()
    house, _, _ = await create(engine_app, user, "casa")
    tip, _, _ = await create(engine_app, user, "telegram", {"identidade_bilhete": None})
    assert (await pairs(engine_app, user))[0].status == "probable"
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await confirmar_par(
            session,
            user,
            await session.get(models.Aposta, house),
            await session.get(models.Aposta, tip),
        )
    row = (await pairs(engine_app, user))[0]
    assert row.status == "excluded"
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        assert (await session.get(models.RevisaoPendente, row.revisao_id)).resolvido_em is not None


async def test_transaction_failure_rolls_back_candidate_and_review(engine_app, novo_usuario):
    user = await novo_usuario()
    await create(engine_app, user, "casa")
    tip, raw, _ = await create(
        engine_app, user, "telegram", {"identidade_bilhete": None}, run=False
    )
    with pytest.raises(RuntimeError):
        async with AsyncSession(engine_app, expire_on_commit=False) as session, session.begin():
            await owner(session, user)
            await generate(session, user, await session.get(models.Aposta, tip), raw)
            raise RuntimeError("synthetic rollback")
    assert await pairs(engine_app, user) == []
    assert await refresh(engine_app, user, tip, raw) == "duvida"


@pytest.mark.parametrize("exact", [False, True])
async def test_real_collection_http_response_reports_persisted_candidate_counts(
    engine_admin, engine_app, novo_usuario, monkeypatch, exact
):
    import httpx
    from fastapi import FastAPI

    from bancaemdia.api.v1 import coleta
    from bancaemdia.db.seed import seed_canonical
    from bancaemdia.db.session import get_db
    from bancaemdia.repositories.coleta_token_repo import ColetaTokenRepo

    async with engine_admin.begin() as conn:
        await seed_canonical(conn)
    user = await novo_usuario()
    body = json.loads((Path(__file__).parents[1] / "fixtures/coleta/betano.json").read_text())
    body["apostas"] = body["apostas"][:1]
    body["apostas"][0]["id"] = uuid4().hex
    parsed = coleta.LEITORES["betano"](body["apostas"][0])
    key = coleta.chave_casa("betano", parsed.identidade)
    await create(engine_app, user, "casa", key=key)
    await create(engine_app, user, "telegram", {} if exact else {"identidade_bilhete": None})
    token = "synthetic-" + uuid4().hex
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await ColetaTokenRepo().create(session, user, coleta.hash_do_token(token))

    async def database():
        async with AsyncSession(engine_app) as session:
            yield session

    app = FastAPI()
    app.include_router(coleta.router)
    app.dependency_overrides[get_db] = database
    published = []
    monkeypatch.setattr(
        coleta.celery, "send_task", lambda *args, **kwargs: published.append(kwargs)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/api/v1/coleta", json=body, headers={coleta.TOKEN_HEADER: token}
            )
            assert response.status_code == 200
            assert response.json()["iguais_a_existentes"] == int(exact)
            assert response.json()["em_duvida"] == int(not exact)
    assert published and len(await pairs(engine_app, user)) == 1


async def test_search_budget_on_one_hundred_thousand_source_rows(
    engine_admin, engine_app, novo_usuario, record_property
):
    user = await novo_usuario()
    # Synthetic workload intentionally includes a busy tenant and records far outside the window.
    async with engine_admin.begin() as conn:
        await conn.execute(
            text("""WITH added AS (
            INSERT INTO apostas(usuario_id,chave,origem,chat_id,message_id,data_aposta,stake_unidades,stake_centavos,odd)
            SELECT CAST(:uid AS bigint),'bench:'||CAST(CAST(:uid AS bigint) AS text)||':'||n,'telegram',CAST(:uid AS bigint),n,now(),1,10000,1.9
            FROM generate_series(1,100000) n RETURNING id,usuario_id,message_id)
            INSERT INTO cruzamento_entradas(aposta_id,usuario_id,casa,origem,ocorrido_em,dados)
            SELECT id,usuario_id,CASE WHEN message_id%10=0 THEN 'Betano' ELSE 'Superbet' END,
            'telegram','2026-09-20T22:30:00Z'::timestamptz - (message_id%2000)*interval '1 day','{}'::jsonb FROM added"""),
            {"uid": user},
        )
        await conn.execute(text("ANALYZE cruzamento_entradas"))
        await conn.execute(text("ANALYZE apostas"))
    entry = Entry(
        aposta_id=-1,
        usuario_id=user,
        origem="casa",
        casa="Betano",
        ocorrido_em=datetime(2026, 9, 20, 22, 30, tzinfo=UTC),
    )
    query = CruzamentoCandidatoRepo().neighbors_query(entry)
    sql = str(query.compile(dialect=engine_app.dialect, compile_kwargs={"literal_binds": True}))
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        plan = (
            await session.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql))
        ).scalar_one()[0]
        results = (await session.scalars(query)).all()
    assert len(results) <= 201
    assert "ix_cruzamento_busca" in json.dumps(plan)
    assert plan["Execution Time"] < 250
    record_property("matching_query_ms_100000_rows", plan["Execution Time"])


async def test_historical_page_backfill_is_resumable_and_financially_read_only(
    engine_app, novo_usuario
):
    from bancaemdia.services.cruzamento_candidatos import rebuild_page

    user = await novo_usuario()
    house, _, _ = await create(engine_app, user, "casa", run=False)
    tip, _, _ = await create(engine_app, user, "telegram", run=False)
    assert await pairs(engine_app, user) == []
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        cursor = await rebuild_page(session, user, limit=1)
        assert cursor == house
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        assert await rebuild_page(session, user, after=cursor, limit=1) == tip
    assert (await pairs(engine_app, user))[0].status == "exact"
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        assert await rebuild_page(session, user, after=tip) == tip
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(models.Evento.usuario_id == user)
            )
            == 2
        )


async def test_any_source_correction_invalidates_stale_exact_eligibility(engine_app, novo_usuario):
    user = await novo_usuario()
    await create(engine_app, user, "casa")
    tip, _, _ = await create(engine_app, user, "telegram")
    assert (await pairs(engine_app, user))[0].status == "exact"
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await session.execute(
            update(models.Aposta).where(models.Aposta.id == tip).values(stake_centavos=999)
        )
    assert (await pairs(engine_app, user))[0].status == "excluded"


async def test_saturation_demotes_hidden_exact_edge_and_cannot_be_bypassed_by_refresh(
    engine_app, novo_usuario
):
    user = await novo_usuario()
    await create(engine_app, user, "casa")
    tip, raw, _ = await create(
        engine_app,
        user,
        "telegram",
        {
            "comeca_em": "2026-09-20T22:31:00Z",
        },
    )
    assert (await pairs(engine_app, user))[0].status == "exact"
    for index in range(200):
        await create(engine_app, user, "telegram", {"identidade_bilhete": f"FILLER-{index}"})
    assert (await pairs(engine_app, user))[0].status == "exact"
    await create(engine_app, user, "casa")
    hidden = (await pairs(engine_app, user))[0]
    assert hidden.status == "probable" and hidden.busca_truncada and hidden.revisao_id
    assert await refresh(engine_app, user, tip, raw) == "duvida"
    assert not any(p.status == "exact" for p in await pairs(engine_app, user))


@pytest.mark.parametrize("confirmed", [False, True])
async def test_only_confirmed_database_aliases_normalize_and_originals_survive(
    engine_admin, engine_app, novo_usuario, confirmed
):
    user = await novo_usuario()
    name, alias = f"Market-{uuid4().hex}", f"Alias-{uuid4().hex}"
    async with AsyncSession(engine_admin) as session, session.begin():
        market = models.Mercado(nome=name)
        session.add(market)
        await session.flush()
        session.add(
            models.Apelido(
                entidade_tipo="mercado", entidade_id=market.id, nome=alias, confirmado=confirmed
            )
        )

    def market_state(value):
        legs = copy.deepcopy(BASE["selecoes"])
        legs[0]["mercado"] = value
        return {"mercado_bruto": value, "selecoes": legs}

    await create(engine_app, user, "casa", market_state(name))
    await create(engine_app, user, "telegram", market_state(alias))
    row = (await pairs(engine_app, user))[0]
    assert row.status == ("exact" if confirmed else "probable")
    assert row.evidencia["telegram"]["original"]["mercado_bruto"] == alias


def test_matching_migration_roundtrip_and_evidence_preservation(isolated_database, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "alembic"))
    monkeypatch.setenv("DATABASE_URL", isolated_database)
    command.upgrade(config, "head")
    command.downgrade(config, "a9d6e3f1c210")
    command.upgrade(config, "head")

    async def populate():
        engine = create_async_engine(isolated_database)
        try:
            async with engine.begin() as conn:
                user = await conn.scalar(
                    text(
                        "INSERT INTO usuarios(email,nome) VALUES('matching@test.invalid','Synthetic') RETURNING id"
                    )
                )
            await create(engine, user, "casa")
            await create(engine, user, "telegram")
        finally:
            await engine.dispose()

    asyncio.run(populate())
    with pytest.raises(DBAPIError, match="preserve matching evidence"):
        command.downgrade(config, "a9d6e3f1c210")
