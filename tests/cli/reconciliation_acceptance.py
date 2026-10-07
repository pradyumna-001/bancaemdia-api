"""Required PostgreSQL acceptance job, on the pinned #110 + current #111 composition.

Explicit filename keeps this dependent suite out of the main-only suite. CI invokes it directly;
missing prerequisite or database is a failure, never a skip.
"""

import asyncio
import copy
import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.cli import reconciliar_casa_telegram as cli
from bancaemdia.cli.reconciliacao_journal import journal
from bancaemdia.cli.reconciliacao_report import ReconciliationError, seal
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.domain.account_attribution_service import account_for_state
from bancaemdia.domain.consolidacao_aposta import unlink
from bancaemdia.services.consolidacao_replay import replay_relations

pytestmark = pytest.mark.xdist_group("postgres")
PLACED = datetime(2026, 9, 20, 12, tzinfo=UTC)
GAME = datetime(2026, 9, 22, 22, tzinfo=UTC)
RAW = {
    "casa": "Betano",
    "identidade_bilhete": "historical-42",
    "tipo_aposta": "SIMPLES",
    "data_aposta": PLACED.isoformat(),
    "data_jogo": GAME.isoformat(),
    "ocorrido_em": PLACED.isoformat(),
    "comeca_em": GAME.isoformat(),
    "evento": "Azul - Verde",
    "mercado_bruto": "Total de gols",
    "descricao": "Mais de 2.5",
    "odd": 2.0,
    "stake_unidades": 1.0,
    "valor_unidade_centavos": 10000,
    "stake_centavos": 10000,
    "valor_aposta_centavos": 10000,
    "estado": "GREEN",
    "retorno_centavos": 20000,
    "freebet": False,
    "selecionada": True,
    "selecoes": [
        {
            "evento": "Azul - Verde",
            "mercado": "Total de gols",
            "escolha": "Mais de 2.5",
            "linha": 2.5,
        }
    ],
}


@pytest.fixture(scope="module", autouse=True)
def require_postgres():
    assert os.environ.get("TEST_DATABASE_URL"), "Required acceptance must use real PostgreSQL"
    cli.runtime()


async def owner(session, user):
    await session.execute(
        text("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(user)}
    )


async def account(
    engine_admin, engine_app, user, *, desde=PLACED - timedelta(days=1), ate=None, usage=True
):
    async with engine_admin.begin() as conn:
        await conn.execute(
            pg_insert(models.Casa)
            .values(nome="Betano", dominio="betano.bet.br")
            .on_conflict_do_nothing()
        )
        house_id = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    async with AsyncSession(engine_app, expire_on_commit=False) as session, session.begin():
        await owner(session, user)
        row = models.ContaCasa(
            usuario_id=user,
            casa_id=house_id,
            apelido="synthetic-" + uuid4().hex,
            desde=desde,
            ate=ate,
        )
        session.add(row)
        await session.flush()
        if usage:
            from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

            window = await UsoContaCasaRepo().open(session, user, house_id, row.id, desde)
            if ate is not None:
                await UsoContaCasaRepo().close(session, window.id, ate)
        return row.id


async def historical(engine, user, origin, *, ticket="historical-42", patch=None, account_id=None):
    state = {
        **copy.deepcopy(RAW),
        "usuario_id": user,
        "origem": origin,
        "identidade_bilhete": ticket,
        **(patch or {}),
    }
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        await owner(session, user)
        bet = models.Aposta(
            usuario_id=user,
            chave=f"{'c' if origin == 'casa' else 't'}:{uuid4().hex}",
            origem=origin,
            chat_id=uuid4().int % (2**60) if origin == "telegram" else None,
            message_id=uuid4().int % (2**60) if origin == "telegram" else None,
            stake_unidades=1.0,
            stake_centavos=10000,
            valor_aposta_centavos=10000,
            odd=state["odd"],
            estado=state["estado"],
            retorno_centavos=state["retorno_centavos"],
            data_aposta=PLACED,
            data_jogo=cli.boundary(state["data_jogo"]),
            conta_casa_id=account_id,
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
        if state["estado"] != "PENDENTE":
            session.add(
                models.Evento(
                    usuario_id=user,
                    aposta_chave=bet.chave,
                    tipo="RESULTADO_REGISTRADO",
                    fonte="liquidacao",
                    payload_json={
                        "estado": state["estado"],
                        "retorno_centavos": state["retorno_centavos"],
                    },
                )
            )
        await session.flush()
        return bet.id


async def fixture_pair(engine_admin, engine_app, novo_usuario, *, tip_patch=None, accounts=True):
    user = await novo_usuario()
    aid = await account(engine_admin, engine_app, user) if accounts else None
    house = await historical(engine_app, user, "casa", account_id=aid)
    tip = await historical(engine_app, user, "telegram", patch=tip_patch, account_id=aid)
    return user, house, tip, aid


async def stored(engine, user):
    async with AsyncSession(engine) as session:
        await owner(session, user)
        return {
            "watermark": await cli.watermark(session, user),
            "totals": await cli.observed_totals(session, user, 100),
            "journals": list(
                (
                    await session.execute(
                        select(journal)
                        .where(journal.c.usuario_id == user)
                        .order_by(journal.c.chunk_index)
                    )
                ).mappings()
            ),
            "relations": await cli.relations(session, user),
            "events": int(
                await session.scalar(
                    select(func.count())
                    .select_from(models.Evento)
                    .where(models.Evento.usuario_id == user)
                )
            ),
        }


async def test_dry_run_is_database_read_only_stable_and_projects_independent_money_oracle(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    before = await stored(engine_app, user)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user), 1)
    after = await stored(engine_app, user)
    assert before == after
    assert reviewed["counts"] == {
        "exact": 1,
        "probable": 0,
        "incompatible": 0,
        "competing": 0,
        "already-consolidated": 0,
        "error": 0,
    }
    assert reviewed["current_totals"]["Betano"] == {
        "bet_count": 2,
        "financial_fact_count": 2,
        "stake": 20000,
        "return": 40000,
        "profit": 20000,
        "unresolved_value": 0,
    }
    assert reviewed["projected_totals"]["Betano"] == {
        "bet_count": 2,
        "financial_fact_count": 1,
        "stake": 10000,
        "return": 20000,
        "profit": 10000,
        "unresolved_value": 0,
    }
    second = await cli.dry_run(engine_app, cli.Filters(user), 1)
    second["generated_at"] = reviewed["generated_at"]
    assert seal(second) == reviewed
    # Independent server enforcement, not a rollback-only pretend dry-run.
    async with AsyncSession(engine_app) as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await owner(session, user)
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        with pytest.raises(DBAPIError):
            await session.execute(
                update(models.Aposta)
                .where(models.Aposta.usuario_id == user)
                .values(stake_centavos=123)
            )


async def test_apply_shared_domain_once_and_replay_rebuilds_same_fact(
    engine_admin, engine_app, novo_usuario
):
    user, house, tip, aid = await fixture_pair(engine_admin, engine_app, novo_usuario)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user), 1)
    result = await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user), 1)
    assert result["complete"] and not result["idempotent"]
    after = await stored(engine_app, user)
    assert after["totals"] == reviewed["projected_totals"]
    assert after["events"] == 6 and len(after["relations"]) == len(after["journals"]) == 1
    link = after["relations"][0]
    assert (
        link.casa_aposta_id == house
        and link.telegram_aposta_id == tip
        and link.conta_casa_id == aid
    )
    again = await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user), 1)
    assert again["idempotent"]
    repeated = await stored(engine_app, user)
    assert repeated["watermark"] == after["watermark"] and repeated["events"] == 6
    async with engine_admin.begin() as conn:
        # Explicit fault injection on disposable synthetic projections, as in #110 acceptance.
        # The product still forbids deletion; restore the guard before running any domain code.
        await conn.execute(
            text("ALTER TABLE aposta_consolidacoes DISABLE TRIGGER consolidation_immutable")
        )
        await conn.execute(
            delete(models.ApostaConsolidacao).where(models.ApostaConsolidacao.usuario_id == user)
        )
        await conn.execute(
            text("ALTER TABLE aposta_consolidacoes ENABLE TRIGGER consolidation_immutable")
        )
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        await replay_relations(session, user)
    replayed = await stored(engine_app, user)
    assert replayed["totals"] == reviewed["projected_totals"]
    assert replayed["relations"][0].id == link.id
    await reconstruir_usuario(user, engine=engine_app)
    assert (await stored(engine_app, user))["totals"] == reviewed["projected_totals"]
    new = await cli.dry_run(engine_app, cli.Filters(user))
    assert new["counts"]["already-consolidated"] == 1


async def test_actual_rollback_keeps_prior_chunk_and_safe_resume(
    engine_admin, engine_app, novo_usuario, monkeypatch
):
    user, _, _, aid = await fixture_pair(engine_admin, engine_app, novo_usuario)
    separate = {"ocorrido_em": (PLACED + timedelta(days=30)).isoformat()}
    await historical(
        engine_app, user, "casa", ticket="historical-99", account_id=aid, patch=separate
    )
    await historical(
        engine_app, user, "telegram", ticket="historical-99", account_id=aid, patch=separate
    )
    reviewed = await cli.dry_run(engine_app, cli.Filters(user), 1)
    partial = await cli.apply_report(
        engine_app, reviewed, reviewed["sha256"], cli.Filters(user), 1, max_chunks=1
    )
    assert not partial["complete"] and partial["next_offset"] == 1
    committed = await stored(engine_app, user)
    real = cli.observed_totals
    count = 0

    async def fail_post_check(*args, **kwargs):
        nonlocal count
        count += 1
        result = await real(*args, **kwargs)
        if count == 2:
            result["Betano"]["stake"] += 1
        return result

    monkeypatch.setattr(cli, "observed_totals", fail_post_check)
    with pytest.raises(ReconciliationError, match="chunk revertido"):
        await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user), 1)
    monkeypatch.setattr(cli, "observed_totals", real)
    failed = await stored(engine_app, user)
    assert failed["watermark"] == committed["watermark"]
    assert failed["journals"] == committed["journals"]
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user), 1)
    final = await stored(engine_app, user)
    assert final["totals"] == reviewed["projected_totals"]
    assert len(final["relations"]) == 2
    assert [row["next_offset"] for row in final["journals"]] == list(
        range(1, len(reviewed["candidates"]) + 1)
    )


@pytest.mark.parametrize(
    "change", ["row", "event", "account", "dictionary", "candidate", "new_competitor"]
)
async def test_stale_source_or_dependency_watermark_refused_before_any_mutation(
    engine_admin, engine_app, novo_usuario, change
):
    user, house, _, aid = await fixture_pair(engine_admin, engine_app, novo_usuario)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    async with AsyncSession(engine_app) as session, session.begin():
        await owner(session, user)
        if change == "row":
            await session.execute(
                update(models.Aposta).where(models.Aposta.id == house).values(odd=3)
            )
        elif change == "event":
            key = await session.scalar(select(models.Aposta.chave).where(models.Aposta.id == house))
            session.add(
                models.Evento(
                    usuario_id=user,
                    aposta_chave=key,
                    tipo="CORRECAO_MANUAL",
                    fonte="manual",
                    payload_json={"descricao": "Changed"},
                )
            )
        elif change == "account":
            await session.execute(
                update(models.ContaCasa).where(models.ContaCasa.id == aid).values(ate=PLACED)
            )
        elif change == "dictionary":
            session.add(
                models.Apelido(
                    entidade_tipo="mercado",
                    entidade_id=1,
                    nome="synthetic-" + uuid4().hex,
                    confirmado=False,
                )
            )
        elif change == "candidate":
            by_id = await cli.prepare_index(session, user, 100)
            for source in by_id.values():
                await cli.runtime().generator.generate(
                    session, user, source["bet"], source["state"]
                )
    if change == "new_competitor":
        await historical(engine_app, user, "telegram", account_id=aid)
    changed = await stored(engine_app, user)
    with pytest.raises(ReconciliationError, match="Fontes alteradas"):
        await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    after = await stored(engine_app, user)
    assert (
        after["watermark"] == changed["watermark"]
        and not after["journals"]
        and not after["relations"]
    )


@pytest.mark.parametrize("kind", ["probable", "competing", "incompatible"])
async def test_ambiguous_candidates_never_change_finance_and_review_is_idempotent(
    engine_admin, engine_app, novo_usuario, kind
):
    patch = (
        {"identidade_bilhete": None}
        if kind == "probable"
        else ({"odd": 3.0} if kind == "incompatible" else None)
    )
    user, _, _, aid = await fixture_pair(engine_admin, engine_app, novo_usuario, tip_patch=patch)
    if kind == "competing":
        await historical(engine_app, user, "telegram", account_id=aid)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    assert reviewed["counts"][kind] >= 1
    assert reviewed["projected_totals"] == reviewed["current_totals"]
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    after = await stored(engine_app, user)
    assert not after["relations"] and after["events"] == (6 if kind == "competing" else 4)
    assert after["totals"] == reviewed["current_totals"]
    if kind != "incompatible":
        async with AsyncSession(engine_app) as session:
            await owner(session, user)
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
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    assert (await stored(engine_app, user))["watermark"] == after["watermark"]


async def test_filter_never_hides_competitor_and_counts_by_game_date(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, aid = await fixture_pair(engine_admin, engine_app, novo_usuario)
    await historical(
        engine_app, user, "casa", account_id=aid, patch={"data_jogo": "2026-09-25T22:00:00+00:00"}
    )
    reviewed = await cli.dry_run(
        engine_app, cli.Filters(user, "2026-09-22", "2026-09-22", "Betano")
    )
    assert reviewed["counts"]["exact"] == 0 and reviewed["counts"]["competing"] == 1
    assert reviewed["projected_totals"] == reviewed["current_totals"]


async def test_default_account_uses_game_date_explicit_multicontas_uses_actual_account(
    engine_admin, engine_app, novo_usuario
):
    user = await novo_usuario()
    placement_account = await account(
        engine_admin, engine_app, user, ate=PLACED + timedelta(hours=1)
    )
    game_account = await account(engine_admin, engine_app, user, desde=PLACED + timedelta(hours=1))
    house = await historical(engine_app, user, "casa", account_id=placement_account)
    await historical(engine_app, user, "telegram", account_id=placement_account)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    link = (await stored(engine_app, user))["relations"][0]
    assert link.casa_aposta_id == house and link.conta_casa_id == game_account
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
        explicit = await account_for_state(
            session, user, {**RAW, "conta_casa_ref": placement_account}, lock=False
        )
        assert explicit.conta_casa_id == placement_account


@pytest.mark.parametrize("kind", ["missing", "ambiguous", "invalid_explicit", "unlinked"])
async def test_account_or_prior_decision_blocked_exact_stays_in_visible_review(
    engine_admin, engine_app, novo_usuario, kind
):
    user, _, _, _aid = await fixture_pair(
        engine_admin, engine_app, novo_usuario, accounts=kind not in {"missing", "ambiguous"}
    )
    if kind == "ambiguous":
        # Two identities without a usage cannot establish a default. PostgreSQL
        # forbids overlapping usages; the pure resolver separately tests ambiguity.
        await account(engine_admin, engine_app, user, usage=False)
        await account(engine_admin, engine_app, user, usage=False)
    if kind == "invalid_explicit":
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            key = await session.scalar(
                select(models.Aposta.chave).where(
                    models.Aposta.usuario_id == user, models.Aposta.origem == "casa"
                )
            )
            session.add(
                models.Evento(
                    usuario_id=user,
                    aposta_chave=key,
                    tipo="CORRECAO_MANUAL",
                    fonte="manual",
                    payload_json={"conta_casa_ref": 9223372036854775807},
                )
            )
    if kind == "unlinked":
        original = await cli.dry_run(engine_app, cli.Filters(user))
        await cli.apply_report(engine_app, original, original["sha256"], cli.Filters(user))
        link = (await stored(engine_app, user))["relations"][0]
        async with AsyncSession(engine_app) as session, session.begin():
            await owner(session, user)
            await unlink(
                session, user, link.id, actor_id=user, reason="synthetic reviewed correction"
            )
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    assert reviewed["counts"]["exact"] == 1 and reviewed["candidates"][0]["action"] == "review"
    assert reviewed["current_totals"] == reviewed["projected_totals"]
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    after = await stored(engine_app, user)
    assert all(link.estado != "active" for link in after["relations"])
    async with AsyncSession(engine_app) as session:
        await owner(session, user)
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


async def test_append_only_journal_is_tenant_isolated_and_rejects_cross_tenant_insert(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    other = await novo_usuario()
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    async with AsyncSession(engine_app) as session:
        await owner(session, other)
        assert not list(await session.execute(select(journal).where(journal.c.usuario_id == user)))
        with pytest.raises(DBAPIError):
            await session.execute(
                insert(journal).values(
                    usuario_id=user,
                    report_sha256="a" * 64,
                    chunk_index=0,
                    next_offset=0,
                    batch_size=1,
                    audit={},
                )
            )
    for command in [update(journal).values(next_offset=99), delete(journal)]:
        async with AsyncSession(engine_app) as session:
            await owner(session, user)
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(command.where(journal.c.usuario_id == user))


async def test_ten_concurrent_apply_retries_create_one_chunk_relation_and_audit(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    results = await asyncio.gather(*[
        cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
        for _ in range(10)
    ])
    assert sum(not result["idempotent"] for result in results) == 1
    after = await stored(engine_app, user)
    assert len(after["relations"]) == len(after["journals"]) == 1 and after["events"] == 6


async def test_resume_refuses_external_changes_preserving_confirmed_checkpoint(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, aid = await fixture_pair(engine_admin, engine_app, novo_usuario)
    await historical(engine_app, user, "casa", ticket="second", account_id=aid)
    await historical(engine_app, user, "telegram", ticket="second", account_id=aid)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user), 1)
    await cli.apply_report(
        engine_app, reviewed, reviewed["sha256"], cli.Filters(user), 1, max_chunks=1
    )
    before = await stored(engine_app, user)
    await historical(engine_app, user, "telegram", ticket="new", account_id=aid)
    with pytest.raises(ReconciliationError, match="checkpoint"):
        await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user), 1)
    assert (await stored(engine_app, user))["journals"] == before["journals"]


@pytest.mark.parametrize("mismatch", ["user", "date", "house", "batch"])
async def test_apply_rejects_mismatched_scope_without_domain_or_checkpoint_writes(
    engine_admin, engine_app, novo_usuario, mismatch
):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    filters = cli.Filters(user)
    batch = 100
    if mismatch == "user":
        filters = cli.Filters(await novo_usuario())
    elif mismatch == "date":
        filters = cli.Filters(user, "2026-09-21")
    elif mismatch == "house":
        filters = cli.Filters(user, casa="Betano")
    else:
        batch = 1
    before = await stored(engine_app, user)
    with pytest.raises(ReconciliationError, match="divergem"):
        await cli.apply_report(engine_app, reviewed, reviewed["sha256"], filters, batch)
    after = await stored(engine_app, user)
    assert after["watermark"] == before["watermark"] and not after["journals"]


async def test_even_rehashed_forged_plan_is_recomputed_not_trusted(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, _ = await fixture_pair(
        engine_admin, engine_app, novo_usuario, tip_patch={"identidade_bilhete": None}
    )
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    reviewed["candidates"][0].update(classification="exact", action="consolidate")
    forged = seal(reviewed)
    with pytest.raises(ReconciliationError, match="Plano diverge"):
        await cli.apply_report(engine_app, forged, forged["sha256"], cli.Filters(user))
    assert not (await stored(engine_app, user))["relations"]


async def test_incomplete_source_history_is_reported_and_apply_refused(
    engine_admin, engine_app, novo_usuario
):
    user, house, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    async with engine_admin.begin() as conn:
        key = await conn.scalar(select(models.Aposta.chave).where(models.Aposta.id == house))
        await conn.execute(
            delete(models.Evento).where(
                models.Evento.usuario_id == user, models.Evento.aposta_chave == key
            )
        )
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    assert reviewed["counts"]["error"] == 1
    with pytest.raises(ReconciliationError, match="fontes com erro"):
        await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    assert not (await stored(engine_app, user))["journals"]


async def test_saturation_veto_includes_hidden_neighbor_beyond_200(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, aid = await fixture_pair(engine_admin, engine_app, novo_usuario)
    for _ in range(200):
        await historical(engine_app, user, "telegram", account_id=aid)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    assert reviewed["counts"]["exact"] == 0 and reviewed["counts"]["competing"] == 200
    assert all(entry["reason"] == "incomplete_search" for entry in reviewed["candidates"])
    assert reviewed["current_totals"] == reviewed["projected_totals"]


async def test_fault_after_real_domain_audit_is_atomic_and_retry_converges(
    engine_admin, engine_app, novo_usuario, monkeypatch
):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    rt = cli.runtime()
    real = rt.service._audit

    async def fault(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("synthetic interruption after domain audit")

    before = await stored(engine_app, user)
    monkeypatch.setattr(rt.service, "_audit", fault)
    with pytest.raises(RuntimeError, match="interruption"):
        await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    monkeypatch.setattr(rt.service, "_audit", real)
    failed = await stored(engine_app, user)
    assert failed["watermark"] == before["watermark"] and failed["events"] == 4
    assert not failed["relations"] and not failed["journals"]
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    assert (await stored(engine_app, user))["totals"] == reviewed["projected_totals"]


async def test_cli_refuses_superuser_or_bypass_credentials(engine_admin, engine_app, novo_usuario):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    with pytest.raises(ReconciliationError, match="BYPASSRLS"):
        await cli.dry_run(engine_admin, cli.Filters(user))
    assert not (await stored(engine_app, user))["journals"]


async def test_populated_journal_migration_refuses_destructive_downgrade(
    engine_admin, engine_app, novo_usuario
):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    reviewed = await cli.dry_run(engine_app, cli.Filters(user))
    await cli.apply_report(engine_app, reviewed, reviewed["sha256"], cli.Filters(user))
    specification = importlib.util.spec_from_file_location(
        "journal_migration", Path("alembic/versions/r111journal2026_reconciliacao_chunks.py")
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    def attempt(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    with pytest.raises(DBAPIError, match="Preserve reconciliation audit"):
        async with engine_admin.begin() as conn:
            await conn.run_sync(attempt)
    after = await stored(engine_app, user)
    assert len(after["journals"]) == 1 and after["totals"] == reviewed["projected_totals"]


async def test_real_cli_subprocess_json_csv_apply_and_safe_rerun(
    engine_admin, engine_app, novo_usuario, tmp_path
):
    user, _, _, _ = await fixture_pair(engine_admin, engine_app, novo_usuario)
    path = tmp_path / "review.json"
    env = {**os.environ, "DATABASE_URL": engine_app.url.render_as_string(hide_password=False)}

    async def invoke(*arguments):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "scripts/reconciliar_casa_telegram.py",
            "--usuario-id",
            str(user),
            "--report",
            str(path),
            *arguments,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        return out.decode()

    from bancaemdia.cli.reconciliacao_report import read_report

    message = await invoke("--csv", str(tmp_path / "review.csv"))
    reviewed = read_report(path)
    assert reviewed["sha256"] in message and (tmp_path / "review.csv").exists()
    assert not (await stored(engine_app, user))["journals"]
    await invoke("--apply", "--sha256", reviewed["sha256"])
    message = await invoke("--apply", "--sha256", reviewed["sha256"])
    assert '"idempotent":true' in message
    assert (await stored(engine_app, user))["totals"] == reviewed["projected_totals"]
