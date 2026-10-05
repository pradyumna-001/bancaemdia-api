"""Real HTTP, PostgreSQL/RLS, durable ACK and financial transaction integration."""

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from test_pairing import real_database_required as real_database_required
from test_pairing import signing_key as signing_key
from test_pairing import system as system

from bancaemdia import models
from bancaemdia.api.v1 import coleta_sessoes
from bancaemdia.models.coleta_sessao import ColetaEntrega, ColetaSessao
from bancaemdia.repositories.coleta_instalacao import owner_scope
from bancaemdia.workers import coleta_v2, materialization

pytestmark = pytest.mark.xdist_group("postgres")
PREFIX = "/api/v1/coleta"


def digest(raw):
    # Independent implementation of the published canonical JSON encoding.
    return hashlib.sha256(
        json.dumps(
            raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def capture(account=None, **changes):
    raw = copy.deepcopy(
        json.loads((Path(__file__).parents[2] / "fixtures/coleta/betano.json").read_text())[
            "apostas"
        ][0]
    )
    raw["id"] = uuid4().hex
    item = {
        "client_event_id": str(uuid4()),
        "hostname": "betano.bet.br",
        "observado": {
            "source": "observed_response",
            "transport": "fetch",
            "method": "GET",
            "path": "/synthetic/history",
            "status": 200,
            "content_type": "application/json",
            "adapter_version": "1.0.0",
            "sanitization_version": 1,
        },
        "capturado_em": "2026-09-01T12:00:00Z",
        "payload": raw,
        "content_hash": digest(raw),
        "conta_casa_ref": account,
    }
    item.update(changes)
    return item


@pytest.fixture
async def api(system, monkeypatch):
    from bancaemdia.config import get_settings

    monkeypatch.setenv("COLETA_RATE_LIMIT", "1000/minute")
    get_settings.cache_clear()
    monkeypatch.setattr(coleta_sessoes, "notify_worker", lambda: None)
    paired = await system.pair()
    headers = {"X-Coleta-Token": paired["token"]}
    async with system.admin.begin() as conn:
        casa = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
        account = await conn.scalar(
            models.ContaCasa.__table__
            .insert()
            .values(
                usuario_id=system.user,
                casa_id=casa,
                apelido="Synthetic",
                desde=datetime(2026, 1, 1, tzinfo=UTC),
            )
            .returning(models.ContaCasa.id)
        )
    boundary = "2026-08-01T00:00:00Z"
    response = await system.http.post(
        PREFIX + "/sessions", headers=headers, json={"coletar_desde": boundary}
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["sessao_id"]

    async def send(items, **changes):
        body = {"contrato": 2, "batch_id": str(uuid4()), "sessao_id": session_id, "items": items}
        body.update(changes)
        return await system.http.post(PREFIX + "/batches", headers=headers, json=body)

    async def status(ack):
        response = await system.http.get(PREFIX + "/jobs/" + ack["job_id"], headers=headers)
        assert response.status_code == 200
        return response.json()

    async def run(ack):
        return await coleta_v2.process_job(system.engine, system.user, UUID(ack["job_id"]))

    yield SimpleNamespace(**{
        **vars(system),
        "headers": headers,
        "paired": paired,
        "account": account,
        "session_id": session_id,
        "boundary": boundary,
        "send": send,
        "run": run,
        "status": status,
    })
    get_settings.cache_clear()


async def test_mixed_batch_stable_ack_and_lossless_raw_replay(api):
    valid = capture(api.account)
    unsafe = capture(api.account)
    unsafe["payload"]["authorization"] = "Bearer SENTINEL"
    unsafe["content_hash"] = digest(unsafe["payload"])
    wrong_hash = capture(api.account, content_hash="0" * 64)
    unknown = capture(api.account, hostname="betano.bet.br.attacker.test")
    before = capture(api.account)
    before["payload"]["placedAt"] = 1767225600000
    before["content_hash"] = digest(before["payload"])
    missing_account = capture()
    items = [valid, unsafe, wrong_hash, unknown, before, missing_account]
    response = await api.send(items)
    assert response.status_code == 200, response.text
    acks = response.json()["items"]
    assert [a["ack"] for a in acks] == [
        "accepted",
        "rejected",
        "rejected",
        "rejected",
        "accepted",
        "accepted",
    ]
    assert [a["reason"] for a in acks[1:4]] == [
        "unsafe_payload",
        "content_hash_mismatch",
        "unsupported_exact_host",
    ]
    assert all(not a["retryable"] for a in acks)
    assert (await api.send(items)).json()["items"] == acks
    assert await api.run(acks[0]) == "materialized"
    assert await api.run(acks[4]) == "ignored_before_boundary"
    assert await api.run(acks[5]) == "needs_review"
    assert (await api.send([valid])).json()["items"] == [acks[0]]
    assert (await api.status(acks[0]))["status"] == "materialized"
    async with api.admin.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(ColetaEntrega.envelope).where(ColetaEntrega.usuario_id == api.user)
                )
            )
            .scalars()
            .all()
        )
        assert "SENTINEL" not in json.dumps(rows)
        assert (
            next(r for r in rows if r and r["client_event_id"] == valid["client_event_id"])[
                "payload"
            ]
            == valid["payload"]
        )
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == api.user)
            )
            == 1
        )
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(models.Evento.usuario_id == api.user, models.Evento.tipo == "APOSTA_CRIADA")
            )
            == 1
        )


async def test_concurrent_timeout_retries_have_one_delivery_and_one_financial_fact(api):
    item = capture(api.account)
    responses = await asyncio.gather(*(api.send([item]) for _ in range(10)))
    assert all(r.status_code == 200 for r in responses)
    acks = [r.json()["items"][0] for r in responses]
    assert all(ack == acks[0] for ack in acks)
    await asyncio.gather(*(api.run(acks[0]) for _ in range(8)))
    assert (await api.status(acks[0]))["status"] == "materialized"
    async with api.admin.connect() as conn:
        for table in (ColetaEntrega, models.ColetaCasa, models.Aposta):
            assert (
                await conn.scalar(
                    select(func.count()).select_from(table).where(table.usuario_id == api.user)
                )
                == 1
            )
    changed = copy.deepcopy(item)
    changed["payload"]["totalAmount"] = 200
    changed["content_hash"] = digest(changed["payload"])
    rejection = (await api.send([changed])).json()["items"][0]
    assert rejection["ack"] == "rejected" and rejection["reason"] == "event_id_conflict"
    assert (await api.send([item])).json()["items"][0] == acks[0]


async def test_two_installations_converge_to_one_financial_fact(api):
    other = await api.pair()
    headers = {"X-Coleta-Token": other["token"]}
    response = await api.http.post(
        PREFIX + "/sessions", headers=headers, json={"coletar_desde": api.boundary}
    )
    assert response.status_code == 200
    item = capture(api.account)
    responses = await asyncio.gather(
        api.send([item]),
        api.http.post(
            PREFIX + "/batches",
            headers=headers,
            json={
                "contrato": 2,
                "batch_id": str(uuid4()),
                "sessao_id": response.json()["sessao_id"],
                "items": [item],
            },
        ),
    )
    assert all(r.status_code == 200 for r in responses)
    results = await asyncio.gather(*(api.run(r.json()["items"][0]) for r in responses))
    assert sorted(results) == ["duplicate", "materialized"]
    async with api.admin.connect() as conn:
        for table, count in ((ColetaEntrega, 2), (models.ColetaCasa, 1), (models.Aposta, 1)):
            assert (
                await conn.scalar(
                    select(func.count()).select_from(table).where(table.usuario_id == api.user)
                )
                == count
            )
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(models.Evento.usuario_id == api.user, models.Evento.tipo == "APOSTA_CRIADA")
            )
            == 1
        )


@pytest.mark.parametrize("bad_revision", ["invalid", "2026-08-02", 99999999999999999999])
async def test_invalid_source_revision_is_not_replaced_by_placement(api, bad_revision):
    item = capture(api.account)
    item["payload"]["settledAt"] = bad_revision
    item["content_hash"] = digest(item["payload"])
    ack = (await api.send([item])).json()["items"][0]
    assert await api.run(ack) == "needs_review"
    assert (await api.status(ack))["reason"] == "source_time_untrusted"
    async with api.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == api.user)
            )
            == 0
        )


async def test_duplicate_content_new_event_and_lifecycle_out_of_order(api):
    settled = capture(api.account)
    opened = copy.deepcopy(settled)
    opened["client_event_id"] = str(uuid4())
    opened["payload"].pop("finalBetResult")
    opened["payload"].pop("settledAt")
    opened["payload"]["finalWinnings"] = 0
    opened["content_hash"] = digest(opened["payload"])
    first = (await api.send([opened])).json()["items"][0]
    assert await api.run(first) == "materialized"
    same = copy.deepcopy(opened)
    same["client_event_id"] = str(uuid4())
    duplicate = (await api.send([same])).json()["items"][0]
    assert duplicate["ack"] == "duplicate"
    assert await api.run(duplicate) == "duplicate"
    last = (await api.send([settled])).json()["items"][0]
    assert await api.run(last) == "updated"
    stale = copy.deepcopy(opened)
    stale["client_event_id"] = str(uuid4())
    old = (await api.send([stale])).json()["items"][0]
    assert await api.run(old) == "duplicate"
    assert (await api.status(old))["reason"] == "stale_source"
    conflict = copy.deepcopy(settled)
    conflict["client_event_id"] = str(uuid4())
    conflict["payload"]["finalBetResult"] = "Lose"
    conflict["content_hash"] = digest(conflict["payload"])
    review = (await api.send([conflict])).json()["items"][0]
    assert await api.run(review) == "needs_review"
    # A v1 client downgrade is still accepted, but its older raw capture cannot undo v2 settlement.
    response = await api.http.post(
        PREFIX,
        headers=api.headers,
        json={"contrato": 1, "casa": "betano", "apostas": [opened["payload"]]},
    )
    assert response.status_code == 200
    async with api.admin.connect() as conn:
        coleta_id = await conn.scalar(
            select(models.ColetaCasa.id).where(models.ColetaCasa.usuario_id == api.user)
        )
    await materialization.gravar_coleta(api.engine, api.user, coleta_id)
    async with api.admin.connect() as conn:
        bet = (
            await conn.execute(
                select(
                    models.Aposta.estado,
                    models.Aposta.retorno_centavos,
                    models.Aposta.conta_casa_id,
                ).where(models.Aposta.usuario_id == api.user)
            )
        ).one()
        assert tuple(bet) == ("GREEN", 30400, api.account)


@pytest.mark.parametrize("protocol", [1, 2])
async def test_same_financial_content_advances_source_clock_without_new_events(api, protocol):
    original = capture(api.account)
    ack = (await api.send([original])).json()["items"][0]
    assert await api.run(ack) == "materialized"
    async with api.admin.connect() as conn:
        events_before = await conn.scalar(
            select(func.count())
            .select_from(models.Evento)
            .where(models.Evento.usuario_id == api.user)
        )
        coleta_id = await conn.scalar(
            select(models.ColetaCasa.id).where(models.ColetaCasa.usuario_id == api.user)
        )
    same = copy.deepcopy(original)
    same["client_event_id"] = str(uuid4())
    same["payload"]["settledAt"] += 7200000
    same["content_hash"] = digest(same["payload"])
    if protocol == 2:
        assert await api.run((await api.send([same])).json()["items"][0]) == "duplicate"
    else:
        response = await api.http.post(
            PREFIX,
            headers=api.headers,
            json={
                "contrato": 1,
                "casa": "betano",
                "apostas": [same["payload"]],
            },
        )
        assert response.status_code == 200
        await materialization.gravar_coleta(api.engine, api.user, coleta_id)
    stale = copy.deepcopy(original)
    stale["client_event_id"] = str(uuid4())
    stale["payload"]["settledAt"] += 3600000
    stale["payload"]["finalBetResult"] = "Lose"
    stale["content_hash"] = digest(stale["payload"])
    ack = (await api.send([stale])).json()["items"][0]
    assert await api.run(ack) == "duplicate"
    assert (await api.status(ack))["reason"] == "stale_source"
    async with api.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(models.Evento.usuario_id == api.user)
            )
            == events_before
        )
        assert await conn.scalar(
            select(models.ColetaCasa.v2_fonte_em).where(models.ColetaCasa.id == coleta_id)
        ) == datetime.fromtimestamp(same["payload"]["settledAt"] / 1000, UTC)
        assert (
            await conn.scalar(
                select(models.Aposta.estado).where(models.Aposta.usuario_id == api.user)
            )
            == "GREEN"
        )


async def test_session_resume_is_explicit_immutable_and_owned_by_installation(api):
    path = PREFIX + "/sessions"
    assert (
        await api.http.post(path, headers=api.headers, json={"coletar_desde": api.boundary})
    ).status_code == 409
    body = {"coletar_desde": api.boundary, "retomar_sessao_id": api.session_id}
    assert (await api.http.post(path, headers=api.headers, json=body)).json()[
        "sessao_id"
    ] == api.session_id
    assert (
        await api.http.post(
            path, headers=api.headers, json={**body, "coletar_desde": "2020-01-01T00:00:00Z"}
        )
    ).status_code == 409
    other_install = await api.pair()
    other_headers = {"X-Coleta-Token": other_install["token"]}
    assert (
        await api.http.get(path + "/" + api.session_id, headers=other_headers)
    ).status_code == 404
    async with AsyncSession(api.engine) as session:
        await owner_scope(session, api.user)
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(
                update(ColetaSessao)
                .where(ColetaSessao.sessao_id == UUID(api.session_id))
                .values(coletar_desde=datetime(2020, 1, 1, tzinfo=UTC))
            )
        await session.rollback()
    item = capture(api.account)
    ack = (await api.send([item])).json()["items"][0]
    assert (
        await api.http.delete(path + "/" + api.session_id, headers=api.headers)
    ).status_code == 204
    assert (await api.send([item])).json()["items"][0] == ack
    assert (await api.send([capture(api.account)])).json()["items"][0]["reason"] == "session_closed"
    assert await api.run(ack) == "materialized"  # Closing does not erase already accepted work.
    new = await api.http.post(
        path, headers=api.headers, json={"coletar_desde": "2026-09-01T00:00:00Z"}
    )
    assert new.status_code == 200 and new.json()["sessao_id"] != api.session_id


@pytest.mark.parametrize(
    "kind", ["foreign", "wrong_house", "before", "ended", "missing", "unknown"]
)
async def test_account_reference_is_never_a_first_active_fallback(api, kind):
    reference = api.account
    async with api.admin.begin() as conn:
        if kind == "foreign":
            await conn.execute(
                update(models.ContaCasa)
                .where(models.ContaCasa.id == reference)
                .values(usuario_id=api.other)
            )
        elif kind == "wrong_house":
            house = await conn.scalar(
                models.Casa.__table__
                .insert()
                .values(nome="Other " + uuid4().hex)
                .returning(models.Casa.id)
            )
            await conn.execute(
                update(models.ContaCasa)
                .where(models.ContaCasa.id == reference)
                .values(casa_id=house)
            )
        elif kind == "before":
            await conn.execute(
                update(models.ContaCasa)
                .where(models.ContaCasa.id == reference)
                .values(desde=datetime(2026, 9, 1, tzinfo=UTC))
            )
        elif kind == "ended":
            await conn.execute(
                update(models.ContaCasa)
                .where(models.ContaCasa.id == reference)
                .values(ate=datetime(2026, 8, 1, tzinfo=UTC))
            )
        elif kind == "missing":
            reference = None
        elif kind == "unknown":
            reference = 2**62
    ack = (await api.send([capture(reference)])).json()["items"][0]
    expected = "materialized" if kind in {"before", "ended"} else "needs_review"
    assert await api.run(ack) == expected
    async with api.admin.connect() as conn:
        assert await conn.scalar(
            select(func.count())
            .select_from(models.Aposta)
            .where(models.Aposta.usuario_id == api.user)
        ) == (1 if expected == "materialized" else 0)


@pytest.mark.parametrize("value", [None, "2026-08-02", "not-a-date", 99999999999999999999])
async def test_capture_clock_cannot_replace_missing_or_untrusted_ticket_time(api, value):
    item = capture(api.account)
    item["payload"]["placedAt"] = value
    item["content_hash"] = digest(item["payload"])
    ack = (await api.send([item])).json()["items"][0]
    assert await api.run(ack) == "needs_review"
    assert (await api.status(ack))["reason"] == "source_time_untrusted"


async def test_rls_jobs_and_transport_ids_do_not_cross_tenants_or_installations(api):
    item = capture(api.account)
    ack = (await api.send([item])).json()["items"][0]
    foreign = await api.pair(uid=api.other)
    same_user = await api.pair()
    for token in (foreign["token"], same_user["token"]):
        assert (
            await api.http.get(PREFIX + "/jobs/" + ack["job_id"], headers={"X-Coleta-Token": token})
        ).status_code == 404
    async with AsyncSession(api.engine) as session:
        await owner_scope(session, api.other)
        assert (
            await session.scalar(
                select(ColetaEntrega.id).where(ColetaEntrega.job_id == UUID(ack["job_id"]))
            )
            is None
        )
        assert (
            await session.scalar(
                select(ColetaSessao.id).where(ColetaSessao.sessao_id == UUID(api.session_id))
            )
            is None
        )
        assert (
            await session.execute(
                update(ColetaEntrega)
                .where(ColetaEntrega.job_id == UUID(ack["job_id"]))
                .values(status="failed")
            )
        ).rowcount == 0


async def test_broker_loss_and_a_fresh_worker_process_recover_persisted_inbox(
    api, monkeypatch, banco
):
    from bancaemdia.services.coleta_ingest import notify_worker
    from bancaemdia.workers.celery_app import app as celery

    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic broker outage")

    monkeypatch.setattr(celery, "send_task", unavailable)
    monkeypatch.setattr(coleta_sessoes, "notify_worker", notify_worker)
    ack = (await api.send([capture(api.account)])).json()["items"][0]
    assert (await api.status(ack))["status"] == "pending"
    script = "import asyncio; from uuid import UUID; from bancaemdia.workers.coleta_v2 import process_job; from bancaemdia.workers.materialization import get_engine; assert asyncio.run(process_job(get_engine(), int(__import__('sys').argv[1]), UUID(__import__('sys').argv[2]))) == 'materialized'"
    env = {**os.environ, "DATABASE_URL": banco.url_app, "DATABASE_URL_REPLICA": banco.url_app}
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(api.user), ack["job_id"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (await api.status(ack))["status"] == "materialized"
    assert await api.run(ack) == "materialized"


async def test_worker_failure_rolls_back_financial_fact_and_retry_materializes_once(
    api, monkeypatch
):
    ack = (await api.send([capture(api.account)])).json()["items"][0]
    original = coleta_v2._gravar_coletada

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise OperationalError("synthetic transaction loss", {}, Exception("simulated"))

    monkeypatch.setattr(coleta_v2, "_gravar_coletada", crash)
    with pytest.raises(OperationalError):
        await api.run(ack)
    async with api.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == api.user)
            )
            == 0
        )
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(models.Evento.usuario_id == api.user)
            )
            == 0
        )
    assert (await api.status(ack))["status"] == "pending"
    monkeypatch.setattr(coleta_v2, "_gravar_coletada", original)
    assert await api.run(ack) == "materialized"


async def test_401_429_unknown_version_and_no_admission_on_failures(api, monkeypatch):
    from bancaemdia.middleware.rate_limit import coleta_limiter

    item = capture(api.account)
    body = {"contrato": 2, "batch_id": str(uuid4()), "sessao_id": api.session_id, "items": [item]}
    assert (await api.http.post(PREFIX + "/batches", json=body)).status_code == 401
    assert (await api.send([item], contrato=3)).status_code == 422
    # Exercise the real limiter with a deliberately exhausted bounded test policy.
    monkeypatch.setenv("COLETA_RATE_LIMIT", "1/minute")
    from bancaemdia.config import get_settings

    get_settings.cache_clear()
    try:
        coleta_limiter.reset()
        first = await api.send([item])
        second = await api.send([capture(api.account)])
        assert first.status_code == 200 and second.status_code == 429
        assert "Retry-After" in second.headers
    finally:
        get_settings.cache_clear()


async def test_daily_limit_rolls_back_batch_and_preserves_original_ack(api, monkeypatch):
    from bancaemdia.config import get_settings

    monkeypatch.setattr(get_settings(), "COLETA_DAILY_LIMIT", 1)
    first, second = capture(api.account), capture(api.account)
    refused = await api.send([first, second])
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) > 0
    async with api.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(ColetaEntrega)
                .where(ColetaEntrega.usuario_id == api.user)
            )
            == 0
        )
    ack = (await api.send([first])).json()["items"][0]
    assert await api.run(ack) == "materialized"
    assert (await api.send([first])).json()["items"] == [ack]
    assert (await api.send([second])).status_code == 429
    legacy = await api.http.post(
        PREFIX,
        headers=api.headers,
        json={
            "contrato": 1,
            "casa": "betano",
            "apostas": [second["payload"]],
        },
    )
    assert legacy.status_code == 429


async def test_daily_limit_is_shared_across_concurrent_installations(api, monkeypatch):
    from bancaemdia.config import get_settings

    monkeypatch.setattr(get_settings(), "COLETA_DAILY_LIMIT", 1)
    other = await api.pair()
    headers = {"X-Coleta-Token": other["token"]}
    response = await api.http.post(
        PREFIX + "/sessions",
        headers=headers,
        json={
            "coletar_desde": api.boundary,
        },
    )
    assert response.status_code == 200
    responses = await asyncio.gather(
        api.send([capture(api.account)]),
        api.http.post(
            PREFIX + "/batches",
            headers=headers,
            json={
                "contrato": 2,
                "batch_id": str(uuid4()),
                "sessao_id": response.json()["sessao_id"],
                "items": [capture(api.account)],
            },
        ),
    )
    assert sorted(r.status_code for r in responses) == [200, 429]
    async with api.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(ColetaEntrega)
                .where(ColetaEntrega.usuario_id == api.user)
            )
            == 1
        )


async def test_hash_tamper_is_terminal_and_never_materializes(api):
    ack = (await api.send([capture(api.account)])).json()["items"][0]
    async with api.admin.begin() as conn:
        await conn.execute(
            update(ColetaEntrega)
            .where(ColetaEntrega.job_id == UUID(ack["job_id"]))
            .values(content_hash="0" * 64)
        )
    assert await api.run(ack) == "failed"
    assert (await api.status(ack))["reason"] == "stored_hash_mismatch"


async def test_http_500_after_commit_retries_without_losing_or_duplicating_capture(
    api, monkeypatch
):
    original = coleta_sessoes.submit

    async def fail_after_commit(*args):
        await original(*args)
        raise RuntimeError("SENTINEL_PRIVATE")

    item = capture(api.account)
    monkeypatch.setattr(coleta_sessoes, "submit", fail_after_commit)
    response = await api.send([item])
    assert response.status_code == 500 and "SENTINEL" not in response.text
    monkeypatch.setattr(coleta_sessoes, "submit", original)
    ack = (await api.send([item])).json()["items"][0]
    assert await api.run(ack) == "materialized"
    assert (await api.send([item])).json()["items"] == [ack]
    async with api.admin.connect() as conn:
        for table in (ColetaEntrega, models.ColetaCasa, models.Aposta):
            assert (
                await conn.scalar(
                    select(func.count()).select_from(table).where(table.usuario_id == api.user)
                )
                == 1
            )


async def test_authenticated_malformed_and_item_size_rejections_have_no_financial_effect(api):
    malformed = await api.http.post(PREFIX + "/batches", headers=api.headers, content=b'{"bad":')
    assert malformed.status_code == 422
    oversized = capture(api.account)
    oversized["payload"]["padding"] = "x" * (128 * 1024)
    oversized["content_hash"] = digest(oversized["payload"])
    response = await api.send([oversized])
    assert response.status_code == 200
    ack = response.json()["items"][0]
    assert ack["ack"] == "rejected" and ack["reason"] == "item_too_large"
    assert ack["job_id"] is None
    assert (await api.send([oversized])).json()["items"] == [ack]
    async with api.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == api.user)
            )
            == 0
        )


async def test_timeout_after_server_commit_repeats_the_original_ack(api):
    import httpx

    from bancaemdia.main import app

    async def discard(message):
        pass

    async def lost_response(scope, receive, send):
        await app(scope, receive, discard)
        raise httpx.ReadTimeout("synthetic response lost after commit")

    item = capture(api.account)
    body = {"contrato": 2, "batch_id": str(uuid4()), "sessao_id": api.session_id, "items": [item]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=lost_response), base_url="https://api.test"
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post(PREFIX + "/batches", headers=api.headers, json=body)
    async with api.admin.connect() as conn:
        original = await conn.scalar(
            select(ColetaEntrega.job_id).where(ColetaEntrega.usuario_id == api.user)
        )
    repeated = (await api.send([item])).json()["items"][0]
    assert repeated["job_id"] == str(original) and repeated["ack"] == "accepted"
    assert await api.run(repeated) == "materialized"


async def test_unexpected_worker_failures_reach_failed_without_partial_financial_writes(
    api, monkeypatch
):
    ack = (await api.send([capture(api.account)])).json()["items"][0]
    original = coleta_v2._gravar_coletada

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic processing failure")

    monkeypatch.setattr(coleta_v2, "_gravar_coletada", fail)
    assert await api.run(ack) == "pending"
    assert await api.run(ack) == "pending"
    assert await api.run(ack) == "failed"
    async with api.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == api.user)
            )
            == 0
        )
        assert (
            await conn.scalar(
                select(ColetaEntrega.tentativas).where(ColetaEntrega.job_id == UUID(ack["job_id"]))
            )
            == 3
        )


async def test_exact_boundary_and_negative_zero_payload_remain_hash_verifiable(api):
    item = capture(api.account)
    item["payload"]["safe_unknown"] = {"signed_zero": -0.0, "unicode": "ação"}
    item["content_hash"] = digest(item["payload"])
    assert (
        await api.http.delete(PREFIX + "/sessions/" + api.session_id, headers=api.headers)
    ).status_code == 204
    boundary = datetime.fromtimestamp(item["payload"]["placedAt"] / 1000, UTC).isoformat()
    response = await api.http.post(
        PREFIX + "/sessions", headers=api.headers, json={"coletar_desde": boundary}
    )
    assert response.status_code == 200
    ack = (await api.send([item], sessao_id=response.json()["sessao_id"])).json()["items"][0]
    assert await api.run(ack) == "materialized"


async def test_v2_migration_roundtrip_and_populated_downgrade_guard(banco_migracao):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    config = Config(str(Path("alembic.ini").resolve()))
    previous = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = banco_migracao.url_admin
    engine = create_async_engine(banco_migracao.url_admin, poolclass=NullPool)
    try:
        await asyncio.to_thread(command.downgrade, config, "c107pair2026")
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.begin() as conn:
            uid = await conn.scalar(
                models.Usuario.__table__
                .insert()
                .values(email="migration@synthetic.test", nome="Synthetic")
                .returning(models.Usuario.id)
            )
            iid = await conn.scalar(
                models.ColetaInstalacao.__table__
                .insert()
                .values(usuario_id=uid, instalacao_publica_id=uuid4())
                .returning(models.ColetaInstalacao.id)
            )
            await conn.execute(
                ColetaSessao.__table__.insert().values(
                    usuario_id=uid,
                    instalacao_id=iid,
                    sessao_id=uuid4(),
                    coletar_desde=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        with pytest.raises(DBAPIError, match="preserve collection sessions"):
            await asyncio.to_thread(command.downgrade, config, "c107pair2026")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version")) == "h5review2026"
            )
            assert await conn.scalar(select(func.count()).select_from(ColetaSessao)) == 1
    finally:
        os.environ["DATABASE_URL"] = previous
        await engine.dispose()
