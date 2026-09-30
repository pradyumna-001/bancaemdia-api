"""Full real v2 intake -> workers -> matching -> finances -> replay matrix.

Explicit filename: CI selects it in the mandatory complete pinned assembly.
"""

import asyncio
import copy
import hashlib
import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from test_consolidacao import (
    GAME,
    financial,
    house_payload,
    intake_telegram,
    owner,
    telegram_payload,
)
from test_contract_v2 import PREFIX, capture, digest

from bancaemdia import models
from bancaemdia.cli import reconciliar_casa_telegram as historical
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.domain import consolidacao_aposta
from bancaemdia.models.coleta_sessao import ColetaEntrega
from bancaemdia.repositories.extrato_repo import ExtratoRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo
from bancaemdia.services import cruzamento_candidatos
from bancaemdia.workers import coleta_v2

pytestmark = pytest.mark.xdist_group("postgres")
FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/coleta_v2/integrity.json").read_text(encoding="utf-8")
)


@pytest.fixture
async def game_account(api):
    async with AsyncSession(api.engine) as session, session.begin():
        await owner(session, api.user)
        house = await session.scalar(
            select(models.ContaCasa.casa_id).where(models.ContaCasa.id == api.account)
        )
        await UsoContaCasaRepo().open(
            session, api.user, house, api.account, GAME - timedelta(days=1)
        )
    return api


def item_for(api, ticket, *, state=None, return_value=200, reference=True):
    raw = house_payload(ticket, state=state, return_value=return_value)
    return capture(
        api.account if reference else None,
        payload=raw,
        content_hash=digest(raw),
        capturado_em="2026-09-30T12:00:00Z",
    )


async def deliver(api, item):
    response = await api.send([item])
    assert response.status_code == 200, response.text
    ack = response.json()["items"][0]
    assert ack["ack"] in {"accepted", "duplicate"} and not ack["retryable"]
    return ack


async def snapshot(api):
    async with AsyncSession(api.engine) as session:
        await owner(session, api.user)
        role = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert tuple(role) == (False, False), "Acceptance must use the restricted application role"
        bets = list(
            await session.scalars(
                select(models.Aposta)
                .where(models.Aposta.usuario_id == api.user)
                .order_by(models.Aposta.id)
            )
        )
        events = (
            await session.execute(
                select(models.Evento.id, models.Evento.tipo, models.Evento.payload_json)
                .where(models.Evento.usuario_id == api.user)
                .order_by(models.Evento.id)
            )
        ).all()
        raw = list(
            await session.scalars(
                select(ColetaEntrega)
                .where(ColetaEntrega.usuario_id == api.user)
                .order_by(ColetaEntrega.id)
            )
        )
        lineages = list(
            await session.scalars(
                select(models.ColetaCasa.identidade).where(models.ColetaCasa.usuario_id == api.user)
            )
        )
        candidates = await session.scalar(
            select(func.count())
            .select_from(models.CruzamentoCandidato)
            .where(models.CruzamentoCandidato.usuario_id == api.user)
        )
        ledger, ledger_count = await ExtratoRepo().list_page(session, api.user, {}, tamanho=200)
    totals, relations = await financial(api.engine, api.user)
    active = [r for r in relations if r.estado == "active"]
    suppressed = {r.telegram_aposta_id for r in active}
    assert ledger_count == sum(
        b.estado != "PENDENTE" and b.selecionada and b.id not in suppressed for b in bets
    )
    assert sum(row.resultado_liquido_centavos for row in ledger) == totals["return"] - (
        totals["stake"] - totals["exposure"]
    )
    for relation in active:
        assert relation.contexto["chat_id"] and relation.contexto["message_id"]
        assert relation.contexto["midia_hash"] == "synthetic-media"
    assert len({r.casa_aposta_id for r in active}) == len(active)
    assert len({r.telegram_aposta_id for r in active}) == len(active)
    assert len(lineages) == len(set(lineages)), "One bookmaker identity must have one lineage"
    assert len({b.chave for b in bets if b.origem == "casa"}) == len(lineages)
    for row in raw:
        if row.envelope is not None:
            assert digest(row.envelope["payload"]) == row.content_hash
            assert row.envelope["observado"]["source"] == "observed_response"
    event_kinds = [kind for _, kind, _ in events]
    assert event_kinds.count("APOSTAS_CONSOLIDADAS") == 2 * len(active)
    return {
        "totals": totals,
        "bets": [
            (
                b.id,
                b.origem,
                b.estado,
                b.stake_centavos,
                b.retorno_centavos,
                b.conta_casa_id,
                b.selecionada,
                b.data_jogo.isoformat() if b.data_jogo else None,
            )
            for b in bets
        ],
        "relations": [
            (r.id, r.estado, r.conta_casa_id, r.evidencia, r.contexto) for r in relations
        ],
        "events_hash": hashlib.sha256(
            json.dumps([tuple(e) for e in events], sort_keys=True, default=str).encode()
        ).hexdigest(),
        "raw_hash": hashlib.sha256(
            json.dumps(
                [(str(r.job_id), r.envelope, r.content_hash) for r in raw], sort_keys=True
            ).encode()
        ).hexdigest(),
        "lineages": len(lineages),
        "deliveries": len(raw),
        "events": len(events),
        "candidates": candidates,
    }


async def invariant(api, request, expected, *, sources=2, lineages=1):
    current = await snapshot(api)
    request.node.integrity_snapshot = {
        "actual": current["totals"],
        "expected": expected,
        "sources": len(current["bets"]),
        "lineages": current["lineages"],
        "deliveries": current["deliveries"],
        "events": current["events"],
        "candidates": current["candidates"],
    }
    assert current["totals"] == expected
    assert len(current["bets"]) == sources
    assert current["lineages"] == lineages
    return current


async def replay_equal(api, request, expected, **changes):
    before = await invariant(api, request, expected, **changes)
    await reconstruir_usuario(api.user, engine=api.engine)
    after = await invariant(api, request, expected, **changes)
    for key in ("bets", "relations", "events_hash", "raw_hash", "lineages"):
        assert after[key] == before[key], f"Replay changed {key}"


def money(returned=0, exposure=10000, count=1, stake=10000):
    return {"count": count, "stake": stake, "return": returned, "exposure": exposure}


@pytest.mark.parametrize("order", ["house", "telegram", "simultaneous"])
async def test_ten_duplicate_deliveries_one_lineage_one_pair_and_full_replay(
    game_account, request, order
):
    api = game_account
    ticket = uuid4().hex
    item, tg = item_for(api, ticket), telegram_payload(ticket)

    async def house():
        responses = await asyncio.gather(*(api.send([item, item]) for _ in range(10)))
        assert all(r.status_code == 200 for r in responses)
        acks = [r.json()["items"] for r in responses]
        assert all(a == [acks[0][0], acks[0][0]] for a in acks)
        await asyncio.wait_for(asyncio.gather(*(api.run(acks[0][0]) for _ in range(10))), 30)
        assert (await api.status(acks[0][0]))["status"] == "materialized"

    if order == "house":
        await house()
        await intake_telegram(api.engine, api.user, tg)
    elif order == "telegram":
        await intake_telegram(api.engine, api.user, tg)
        await house()
    else:
        await asyncio.wait_for(
            asyncio.gather(house(), intake_telegram(api.engine, api.user, tg)), 30
        )
    await replay_equal(api, request, money())
    assert (await snapshot(api))["deliveries"] == 1


@pytest.mark.parametrize("outcome", ["win", "cancelled", "cashout"])
@pytest.mark.parametrize("order", ["open-first", "settled-first", "racing"])
async def test_lifecycle_stale_open_duplicate_settled_and_consolidation_race(
    game_account, request, outcome, order
):
    api = game_account
    ticket = uuid4().hex
    fixture = FIXTURE[outcome]
    opened = item_for(api, ticket)
    settled = item_for(api, ticket, state=fixture["state"], return_value=fixture["return"] / 100)
    first, last = await deliver(api, opened), await deliver(api, settled)
    tg = telegram_payload(ticket)
    if order == "open-first":
        await api.run(first)
        await intake_telegram(api.engine, api.user, tg)
        await api.run(last)
    elif order == "settled-first":
        await api.run(last)
        await intake_telegram(api.engine, api.user, tg)
        await api.run(first)
    else:
        await asyncio.wait_for(
            asyncio.gather(
                api.run(first), api.run(last), intake_telegram(api.engine, api.user, tg)
            ),
            30,
        )
    duplicate = copy.deepcopy(settled)
    duplicate["client_event_id"] = str(uuid4())
    assert await api.run(await deliver(api, duplicate)) == "duplicate"
    old = copy.deepcopy(opened)
    old["client_event_id"] = str(uuid4())
    assert await api.run(await deliver(api, old)) == "duplicate"
    await replay_equal(api, request, money(fixture["return"], fixture["exposure"]))
    assert (await snapshot(api))["lineages"] == 1


@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_occurrence_boundary_capture_today_does_not_override_cutoff(api, request, offset):
    boundary = datetime.fromisoformat(api.boundary.replace("Z", "+00:00"))
    item = item_for(api, uuid4().hex)
    item["payload"]["placedAt"] = int((boundary + timedelta(seconds=offset)).timestamp() * 1000)
    item["content_hash"] = digest(item["payload"])
    ack = await deliver(api, item)
    assert await api.run(ack) == ("ignored_before_boundary" if offset < 0 else "materialized")
    await replay_equal(
        api,
        request,
        money(count=0, stake=0, exposure=0) if offset < 0 else money(),
        sources=0 if offset < 0 else 1,
        lineages=0 if offset < 0 else 1,
    )


@pytest.mark.parametrize("value", [None, "2026-09-20", "invalid"])
async def test_untrusted_occurrence_review_has_no_financial_fact(api, request, value):
    item = item_for(api, uuid4().hex)
    item["payload"]["placedAt"] = value
    item["content_hash"] = digest(item["payload"])
    ack = await deliver(api, item)
    assert await api.run(ack) == "needs_review"
    await replay_equal(api, request, money(count=0, stake=0, exposure=0), sources=0, lineages=0)


async def test_mixed_ack_retry_only_unacknowledged_preserves_success_subset(
    game_account, request, monkeypatch
):
    api = game_account
    ticket = uuid4().hex
    accepted = item_for(api, ticket)
    rejected = item_for(api, uuid4().hex)
    rejected["content_hash"] = "0" * 64
    acks = (await api.send([accepted, rejected])).json()["items"]
    assert [a["ack"] for a in acks] == ["accepted", "rejected"]
    assert all(not a["retryable"] for a in acks)
    await api.run(acks[0])
    await intake_telegram(api.engine, api.user, telegram_payload(ticket))
    before = await snapshot(api)
    # A rejected terminal ACK is not retried; only a lost response is retried with the original ID.
    from bancaemdia.api.v1 import coleta_sessoes

    original = coleta_sessoes.submit
    unseen = copy.deepcopy(accepted)
    unseen["client_event_id"] = str(uuid4())

    async def lost(*args):
        await original(*args)
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(coleta_sessoes, "submit", lost)
    assert (await api.send([unseen])).status_code == 500
    monkeypatch.setattr(coleta_sessoes, "submit", original)
    retry = await deliver(api, unseen)
    assert retry["ack"] == "duplicate"
    assert await api.run(retry) == "duplicate"
    assert (await snapshot(api))["events_hash"] == before["events_hash"]
    await replay_equal(api, request, money())
    assert (await snapshot(api))["deliveries"] == 3


@pytest.mark.parametrize("malformed", ["json", "items", "body"])
async def test_entire_malformed_or_oversized_batch_cannot_commit_prefix(api, request, malformed):
    item = item_for(api, uuid4().hex)
    body = {"contrato": 2, "batch_id": str(uuid4()), "sessao_id": api.session_id, "items": [item]}
    if malformed == "json":
        response = await api.http.post(
            PREFIX + "/batches", headers=api.headers, content=b'{"items":['
        )
        assert response.status_code == 422
    elif malformed == "items":
        body["items"] = [item] * 101
        response = await api.http.post(PREFIX + "/batches", headers=api.headers, json=body)
        assert response.status_code == 422
    else:
        item["payload"]["padding"] = "x" * (3 * 1024 * 1024)
        response = await api.http.post(PREFIX + "/batches", headers=api.headers, json=body)
        assert response.status_code == 413
    await invariant(api, request, money(count=0, stake=0, exposure=0), sources=0, lineages=0)
    assert (await snapshot(api))["deliveries"] == 0


@pytest.mark.parametrize("fault", ["collection", "candidate", "consolidation"])
async def test_fault_between_stages_rolls_back_then_retry_and_replay_converge(
    game_account, request, monkeypatch, fault
):
    api = game_account
    ticket = uuid4().hex
    await intake_telegram(api.engine, api.user, telegram_payload(ticket))
    before = await snapshot(api)
    ack = await deliver(api, item_for(api, ticket))
    target, name = {
        "collection": (coleta_v2, "_gravar_coletada"),
        "candidate": (cruzamento_candidatos, "generate"),
        "consolidation": (consolidacao_aposta, "_audit"),
    }[fault]
    original = getattr(target, name)

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise OperationalError("synthetic rollback", {}, Exception("synthetic"))

    monkeypatch.setattr(target, name, crash)
    with pytest.raises(OperationalError):
        await api.run(ack)
    after = await snapshot(api)
    for key in ("bets", "relations", "events_hash", "lineages", "candidates"):
        assert after[key] == before[key], f"Partial write after {fault}: {key}"
    assert (await api.status(ack))["status"] == "pending"
    monkeypatch.setattr(target, name, original)
    assert await api.run(ack) == "materialized"
    await replay_equal(api, request, money())


@pytest.mark.parametrize("seed", FIXTURE["seeds"])
async def test_seeded_duplicate_update_arrival_interleavings(game_account, request, seed):
    api = game_account
    randomizer = random.Random(seed + int(os.environ.get("INTEGRITY_SEED", "0")))
    ticket = uuid4().hex
    opened = item_for(api, ticket)
    settled = item_for(api, ticket, state="Win")
    acks = [await deliver(api, item) for item in (opened, settled)]
    actions = ["telegram", 0, 1, 0, 1, 1]
    randomizer.shuffle(actions)

    async def run(action, delay):
        await asyncio.sleep(delay)
        if action == "telegram":
            await intake_telegram(api.engine, api.user, telegram_payload(ticket))
        else:
            await api.run(acks[action])

    await asyncio.wait_for(
        asyncio.gather(*[run(action, randomizer.random() / 100) for action in actions]), 30
    )
    await replay_equal(api, request, money(20000, 0))


async def test_redis_broker_hint_and_redelivery_use_durable_database_inbox(
    game_account, request, monkeypatch
):
    from bancaemdia.api.v1 import coleta_sessoes
    from bancaemdia.services.coleta_ingest import notify_worker
    from bancaemdia.workers.celery_app import app as celery

    api = game_account
    ticket = uuid4().hex
    queue = "integrity:" + uuid4().hex
    broker = os.environ["REDIS_URL"]
    # Publish through Celery's real Redis transport; no external worker/task/AI is used.
    with celery.connection_for_write(url=broker) as connection:
        with celery.producer_or_acquire(connection.Producer()) as producer:

            def send_task(name):
                return celery.send_task(name, producer=producer, queue=queue)

            monkeypatch.setattr(coleta_sessoes, "notify_worker", notify_worker)
            original = celery.send_task

            def publish(name):
                return original(name, producer=producer, queue=queue)

            monkeypatch.setattr(celery, "send_task", publish)
            ack = await deliver(api, item_for(api, ticket))
    async with Redis.from_url(broker) as redis:
        try:
            assert await redis.llen(queue) == 1
            message = await redis.lpop(queue)
            decoded = json.loads(message)
            assert decoded["headers"]["task"] == "materialization.collection_v2"
            # A lost hint is recovered by the same inbox, and duplicate hint delivery is harmless.
            await redis.rpush(queue, message, message)
            assert await redis.llen(queue) == 2
            await api.run(ack)
            await api.run(ack)
        finally:
            await redis.delete(queue, "_kombu.binding." + queue)
    await intake_telegram(api.engine, api.user, telegram_payload(ticket))
    await replay_equal(api, request, money())


async def test_historical_cli_reuses_online_pair_and_does_not_duplicate_audit(
    game_account, request
):
    api = game_account
    ticket = uuid4().hex
    await api.run(await deliver(api, item_for(api, ticket, state="Win")))
    await intake_telegram(api.engine, api.user, telegram_payload(ticket))
    filters = historical.Filters(api.user)
    before = await snapshot(api)
    report = await historical.dry_run(api.engine, filters, batch_size=1)
    assert (await snapshot(api)) == before
    await historical.apply_report(api.engine, report, report["sha256"], filters, batch_size=1)
    await historical.apply_report(api.engine, report, report["sha256"], filters, batch_size=1)
    after = await snapshot(api)
    assert after["events_hash"] == before["events_hash"]
    await replay_equal(api, request, money(20000, 0))
