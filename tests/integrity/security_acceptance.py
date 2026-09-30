"""Real HTTP token races and tenant isolation, with deterministic database barriers."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_contract_v2 import PREFIX

from bancaemdia import models
from bancaemdia.api.v1 import coleta_sessoes
from bancaemdia.models.coleta_sessao import ColetaEntrega, ColetaSessao
from bancaemdia.repositories.coleta_instalacao import ColetaInstalacaoRepo

from .collection_acceptance import deliver, invariant, item_for, money, snapshot

pytestmark = pytest.mark.xdist_group("postgres")


async def blocked(admin):
    for _ in range(100):
        async with admin.connect() as connection:
            if await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname=current_database() AND cardinality(pg_blocking_pids(pid))>0)"
                )
            ):
                return
        await asyncio.sleep(0.01)
    raise AssertionError("The competing request did not reach the real row lock")


@pytest.mark.parametrize("revoke", [False, True])
async def test_rotation_or_revocation_wins_before_collection_commit(
    api, system, request, monkeypatch, revoke
):
    locked, release = asyncio.Event(), asyncio.Event()
    original = ColetaInstalacaoRepo.owned

    async def hold(*args):
        result = await original(*args)
        locked.set()
        await release.wait()
        return result

    monkeypatch.setattr(ColetaInstalacaoRepo, "owned", hold)
    rotating = asyncio.create_task(
        api.http.request(
            "DELETE" if revoke else "POST",
            PREFIX
            + f"/installations/{api.paired['instalacao_id']}"
            + ("" if revoke else "/rotate"),
            headers=system.headers(),
        )
    )
    await asyncio.wait_for(locked.wait(), 10)
    collecting = asyncio.create_task(api.send([item_for(api, uuid4().hex)]))
    try:
        await blocked(api.admin)
        assert not collecting.done()
    finally:
        release.set()
    response, refused = await asyncio.wait_for(asyncio.gather(rotating, collecting), 20)
    assert response.status_code == (204 if revoke else 200)
    assert refused.status_code == 403
    assert (await api.send([item_for(api, uuid4().hex)])).status_code == 403
    await invariant(api, request, money(count=0, stake=0, exposure=0), sources=0, lineages=0)
    assert (await snapshot(api))["deliveries"] == 0


@pytest.mark.parametrize("revoke", [False, True])
async def test_accepted_transaction_commits_before_revocation_returns(
    api, system, request, monkeypatch, revoke
):
    authenticated, release = asyncio.Event(), asyncio.Event()
    original = coleta_sessoes.submit

    async def hold(*args):
        authenticated.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(coleta_sessoes, "submit", hold)
    collecting = asyncio.create_task(api.send([item_for(api, uuid4().hex)]))
    await asyncio.wait_for(authenticated.wait(), 10)
    rotating = asyncio.create_task(
        api.http.request(
            "DELETE" if revoke else "POST",
            PREFIX
            + f"/installations/{api.paired['instalacao_id']}"
            + ("" if revoke else "/rotate"),
            headers=system.headers(),
        )
    )
    try:
        await blocked(api.admin)
        assert not rotating.done()
    finally:
        release.set()
    admitted, changed = await asyncio.wait_for(asyncio.gather(collecting, rotating), 20)
    assert admitted.status_code == 200 and changed.status_code == (204 if revoke else 200)
    assert (await api.send([item_for(api, uuid4().hex)])).status_code == 403
    ack = admitted.json()["items"][0]
    assert await api.run(ack) == "materialized"
    await invariant(api, request, money(), sources=1)
    assert (await snapshot(api))["deliveries"] == 1


@pytest.mark.parametrize("kind", ["foreign", "wrong_house", "unknown"])
async def test_invalid_explicit_account_never_falls_back_to_a_valid_account(api, request, kind):
    reference = api.account
    async with api.admin.begin() as connection:
        if kind == "foreign":
            await connection.execute(
                update(models.ContaCasa)
                .where(models.ContaCasa.id == reference)
                .values(usuario_id=api.other)
            )
        elif kind == "wrong_house":
            house = await connection.scalar(
                models.Casa.__table__
                .insert()
                .values(nome="Synthetic " + uuid4().hex)
                .returning(models.Casa.id)
            )
            await connection.execute(
                update(models.ContaCasa)
                .where(models.ContaCasa.id == reference)
                .values(casa_id=house)
            )
        else:
            reference = 2**62
        await connection.execute(
            models.ContaCasa.__table__.insert().values(
                usuario_id=api.user,
                casa_id=await connection.scalar(
                    select(models.Casa.id).where(models.Casa.nome == "Betano")
                ),
                apelido="Valid fallback trap",
                desde=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    item = item_for(api, uuid4().hex)
    item["conta_casa_ref"] = reference
    assert await api.run(await deliver(api, item)) == "needs_review"
    await invariant(api, request, money(count=0, stake=0, exposure=0), sources=0, lineages=0)


async def test_collection_credentials_cannot_read_mutate_replay_or_own_foreign_rows(api, request):
    ack = await deliver(api, item_for(api, uuid4().hex))
    await api.run(ack)
    foreign = await api.pair(uid=api.other)
    other_installation = await api.pair()
    before = await snapshot(api)
    for credential in (foreign, other_installation):
        headers = {"X-Coleta-Token": credential["token"]}
        assert (
            await api.http.get(PREFIX + "/jobs/" + ack["job_id"], headers=headers)
        ).status_code == 404
        assert (
            await api.http.get(PREFIX + "/sessions/" + api.session_id, headers=headers)
        ).status_code == 404
        assert (
            await api.http.delete(PREFIX + "/sessions/" + api.session_id, headers=headers)
        ).status_code == 404
        assert (
            await api.http.post(
                PREFIX + "/batches",
                headers=headers,
                json={
                    "contrato": 2,
                    "batch_id": str(uuid4()),
                    "sessao_id": api.session_id,
                    "items": [item_for(api, uuid4().hex)],
                },
            )
        ).status_code == 404
    for method, path, body in (
        ("GET", "/api/v1/apostas", None),
        ("POST", "/api/v1/apostas", {"casa": "Betano", "odd": 2, "stake_unidades": 1}),
        ("POST", PREFIX + "/pairing-codes", None),
        ("POST", PREFIX + f"/installations/{foreign['instalacao_id']}/rotate", None),
    ):
        assert (
            await api.http.request(method, path, headers=api.headers, json=body)
        ).status_code == 401
    assert await coleta_job_for_other(api, ack) is None
    async with AsyncSession(api.engine) as session:
        from test_consolidacao import owner

        await owner(session, api.other)
        for model in (ColetaEntrega, ColetaSessao, models.ColetaCasa, models.Aposta, models.Evento):
            assert (
                list(await session.scalars(select(model).where(model.usuario_id == api.user))) == []
            )
            assert (
                await session.execute(
                    update(model).where(model.usuario_id == api.user).values(usuario_id=api.other)
                )
            ).rowcount == 0
        await session.rollback()
    assert (await snapshot(api)) == before
    await invariant(api, request, money(), sources=1)


async def coleta_job_for_other(api, ack):
    from uuid import UUID

    from bancaemdia.workers.coleta_v2 import process_job

    return await process_job(api.engine, api.other, UUID(ack["job_id"]))
