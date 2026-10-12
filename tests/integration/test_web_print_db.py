"""Mandatory real API, forced PostgreSQL RLS and real web worker proofs."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from kombu.exceptions import OperationalError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db, get_db_primary_snapshot
from bancaemdia.models.web_print import PrintAsset, PrintItem
from bancaemdia.services.web_print import now
from bancaemdia.workers import web_print as worker
from tests.integration import test_aposta_foto_db as photo_support
from tests.integration.test_aposta_foto_db import Api, image_bytes, private, snapshot, url
from tests.integration.test_telegram_confirmation import _account

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


@pytest.fixture
async def api(engine_app: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    from bancaemdia.main import app

    async def database() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id',:uid,true)"),
                {"uid": current_user_id.get() or ""},
            )
            yield session

    async for underlying in photo_support.api.__wrapped__(engine_app, monkeypatch):
        before = app.dependency_overrides[get_db]
        app.dependency_overrides[get_db] = database
        monkeypatch.setattr("bancaemdia.api.v1.web_print.celery.send_task", lambda *a, **kw: None)
        try:
            yield underlying
        finally:
            app.dependency_overrides[get_db] = before


def item_url(intent: UUID, item: UUID) -> str:
    return f"/api/v1/prints/intencoes/{intent}/itens/{item}"


async def submit(
    api: Api,
    user: int,
    content: bytes,
    intent: UUID | None = None,
    item: UUID | None = None,
    mime: str = "image/png",
) -> tuple[UUID, UUID, Any]:
    intent, item = intent or uuid4(), item or uuid4()
    response = await api.client.post(
        item_url(intent, item),
        headers=api.headers(user),
        files={"file": ("../../private.png", content, mime)},
    )
    return intent, item, response


async def asset_id(engine: AsyncEngine, como: Any, user: int, intent: UUID, item: UUID) -> UUID:
    async with como(engine, user) as session:
        row = await session.get(PrintItem, (user, intent, item))
        assert row is not None
        return row.asset_id


def reading(*, stake: float | None = 1.0) -> dict[str, Any]:
    return {
        "bilhete": {
            "casa": "Betano",
            "tipo": "simples",
            "evento": "Casa x Fora",
            "selecoes": [{"mercado": "Resultado", "escolha": "Casa"}],
            "odd_total": 2.0,
            "quando": "2026-10-13T12:00:00-03:00",
            "stake_unidades": stake,
            "confianca": 0.99,
        },
        "grave": False,
    }


@pytest.fixture
def extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "extrair_bilhete", lambda *a, **kw: reading())


async def test_multiple_files_real_worker_and_no_financial_replay(
    api, engine_app, engine_admin, como, novo_usuario, extractor
):
    user = await novo_usuario()
    await _account(engine_admin, engine_app, como, user)
    intent = uuid4()
    identities = []
    for _ in range(2):
        i, item, response = await submit(api, user, image_bytes(), intent)
        assert response.status_code == 202, response.text
        assert response.json()["results"] is None and response.json()["outcome"] is None
        assert response.json()["progress_percent"] is None
        private(response)
        identity = await asset_id(engine_app, como, user, i, item)
        assert await worker.process_once(engine_app, user, identity)
        identities.append((i, item, identity))
    result = await api.client.get(f"/api/v1/prints/intencoes/{intent}", headers=api.headers(user))
    assert result.status_code == 200 and result.json()["state"] == "complete"
    assert result.json()["terminal_items"] == 2
    assert all(item["outcome"] == "materialized" for item in result.json()["items"])
    for item in result.json()["items"]:
        photo = await api.client.get(url(item["results"][0]["bet_key"]), headers=api.headers(user))
        assert photo.status_code == 200
        private(photo)
    before = await snapshot(engine_app, como, user)
    assert len(before["apostas"]) == 2
    for _, _, identity in identities:
        assert not await worker.process_once(engine_app, user, identity)
    assert await snapshot(engine_app, como, user) == before


async def test_repeated_and_concurrent_identical_bytes_dedupe_per_user(
    api, engine_app, como, novo_usuario, extractor
):
    user, other = await novo_usuario(), await novo_usuario()
    content, intent = image_bytes(), uuid4()
    submissions = await asyncio.gather(*(submit(api, user, content, intent) for _ in range(4)))
    assert all(r.status_code == 202 for _, _, r in submissions), [r.text for _, _, r in submissions]
    ids = [await asset_id(engine_app, como, user, i, item) for i, item, _ in submissions]
    assert len(set(ids)) == 1
    assert sum(r.json()["duplicate"] for _, _, r in submissions) == 3
    await worker.process_once(engine_app, user, ids[0])
    before = await snapshot(engine_app, como, user)
    i, item, _ = submissions[0]
    for _ in range(2):
        assert (await submit(api, user, content, i, item))[2].status_code == 202
    other_i, other_item, r = await submit(api, other, content)
    assert r.status_code == 202 and not r.json()["duplicate"]
    assert await asset_id(engine_app, como, other, other_i, other_item) != ids[0]
    foreign = await api.client.get(item_url(i, item), headers=api.headers(other))
    missing = await api.client.get(item_url(uuid4(), uuid4()), headers=api.headers(other))
    assert foreign.status_code == missing.status_code == 404 and foreign.json() == missing.json()
    assert await snapshot(engine_app, como, user) == before
    async with como(engine_app, other) as session:
        assert await session.get(PrintAsset, ids[0]) is None


async def test_lost_post_response_and_broker_outage_reconcile_without_replay(
    api, engine_app, como, novo_usuario, monkeypatch, extractor
):
    def unavailable(*a, **kw):
        raise OperationalError("synthetic transport failure")

    monkeypatch.setattr("bancaemdia.api.v1.web_print.celery.send_task", unavailable)
    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    reconciled = await api.client.get(item_url(intent, item), headers=api.headers(user))
    assert reconciled.status_code == 200 and reconciled.json()["status"] == "accepted"
    identity = await asset_id(engine_app, como, user, intent, item)
    sent = []
    monkeypatch.setattr(worker.app, "send_task", lambda name, **kw: sent.append((name, kw)))
    await worker.dispatch(engine_app)
    assert any(kw["kwargs"] == {"usuario_id": user, "asset_id": str(identity)} for _, kw in sent)
    assert all(set(kw["kwargs"]) == {"usuario_id", "asset_id"} for _, kw in sent)
    assert await worker.process_once(engine_app, user, identity)
    final = await api.client.get(item_url(intent, item), headers=api.headers(user))
    assert final.json()["status"] == "completed"


async def test_partial_failure_explicit_single_item_retry_and_same_retry_identity(
    api, engine_app, como, novo_usuario, monkeypatch
):
    user, intent = await novo_usuario(), uuid4()
    ok_i, ok_item, r = await submit(api, user, image_bytes(), intent)
    assert r.status_code == 202
    ok_id = await asset_id(engine_app, como, user, ok_i, ok_item)
    monkeypatch.setattr(worker, "extrair_bilhete", lambda *a, **kw: reading())
    await worker.process_once(engine_app, user, ok_id)
    fail_i, fail_item, r = await submit(api, user, image_bytes(), intent)
    fail_id = await asset_id(engine_app, como, user, fail_i, fail_item)

    def unavailable(*a, **kw):
        raise ConnectionError("synthetic unavailable")

    monkeypatch.setattr(worker, "extrair_bilhete", unavailable)
    await worker.process_once(engine_app, user, fail_id)
    state = await api.client.get(f"/api/v1/prints/intencoes/{intent}", headers=api.headers(user))
    assert state.json()["state"] == "partial"
    failure = await api.client.get(item_url(fail_i, fail_item), headers=api.headers(user))
    assert failure.json()["retryable"] and failure.json()["error_code"] == "unavailable"
    body = {"retry_id": str(uuid4()), "expected_attempt": 1}
    forbidden = await api.client.post(
        item_url(ok_i, ok_item) + "/retry", json=body, headers=api.headers(user)
    )
    assert forbidden.status_code == 409
    retried = await api.client.post(
        item_url(fail_i, fail_item) + "/retry", json=body, headers=api.headers(user)
    )
    assert retried.status_code == 202 and retried.json()["attempt"] == 2
    monkeypatch.setattr(worker, "extrair_bilhete", lambda *a, **kw: reading())
    await worker.process_once(engine_app, user, fail_id)
    before = await snapshot(engine_app, como, user)
    repeated = await api.client.post(
        item_url(fail_i, fail_item) + "/retry", json=body, headers=api.headers(user)
    )
    assert repeated.status_code == 202 and repeated.json()["status"] == "completed"
    assert not await worker.process_once(engine_app, user, fail_id)
    assert await snapshot(engine_app, como, user) == before


async def test_identity_incompatible_bytes_is_deterministic_and_preserves_original(
    api, engine_app, como, novo_usuario
):
    user, content = await novo_usuario(), image_bytes()
    intent, item, r = await submit(api, user, content)
    assert r.status_code == 202
    before = await snapshot(engine_app, como, user)
    for _ in range(2):
        assert (await submit(api, user, image_bytes(), intent, item))[2].status_code == 409
    identity = await asset_id(engine_app, como, user, intent, item)
    from bancaemdia.services.web_print import cipher

    async with como(engine_app, user) as session:
        asset = await session.get(PrintAsset, identity)
        assert cipher().decrypt(asset.ciphertext) == content
    assert await snapshot(engine_app, como, user) == before


@pytest.mark.parametrize("action", ["CORRIGIR", "DESCARTAR"])
async def test_unknown_stake_goes_to_real_review_without_zero_bet_and_can_resolve(
    api, engine_app, como, novo_usuario, monkeypatch, action
):
    user, content = await novo_usuario(), image_bytes()
    monkeypatch.setattr(worker, "extrair_bilhete", lambda *a, **kw: reading(stake=None))
    intent, item, r = await submit(api, user, content)
    assert r.status_code == 202
    await worker.process_once(
        engine_app, user, await asset_id(engine_app, como, user, intent, item)
    )
    state = (await api.client.get(item_url(intent, item), headers=api.headers(user))).json()
    assert state["outcome"] == "review" and state["results"][0]["bet_key"] is None
    facts = await snapshot(engine_app, como, user)
    assert not facts["apostas"] and not facts["eventos"]
    review_id = state["results"][0]["review_ids"][0]
    detail = await api.client.get(f"/api/v1/revisao/{review_id}", headers=api.headers(user))
    assert detail.json()["extracao_bruta"]["campos"]["stake_unidades"] is None
    photo = await api.client.get(f"/api/v1/revisao/{review_id}/foto", headers=api.headers(user))
    assert photo.status_code == 200 and photo.content == content
    body = {"acao": action}
    if action == "CORRIGIR":
        invalid = await api.client.post(
            f"/api/v1/prints/revisoes/{review_id}/resolver", headers=api.headers(user), json=body
        )
        assert invalid.status_code == 422
        body["aposta_corrigida"] = {"stake_unidades": 1.0}
    resolved = await api.client.post(
        f"/api/v1/prints/revisoes/{review_id}/resolver", headers=api.headers(user), json=body
    )
    assert resolved.status_code == 200, resolved.text
    if action == "CORRIGIR":
        key = resolved.json()["aposta"]["chave"]
        assert (await api.client.get(url(key), headers=api.headers(user))).content == content
    else:
        assert resolved.json()["aposta"] is None
        assert not (await snapshot(engine_app, como, user))["apostas"]
    repeat = await api.client.post(
        f"/api/v1/prints/revisoes/{review_id}/resolver", headers=api.headers(user), json=body
    )
    assert repeat.status_code == 409


async def test_real_no_bet_result_is_distinct_from_failure(
    api, engine_app, como, novo_usuario, monkeypatch
):
    monkeypatch.setattr(worker, "extrair_bilhete", lambda *a, **kw: {"nao_e_aposta": True})
    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    await worker.process_once(
        engine_app, user, await asset_id(engine_app, como, user, intent, item)
    )
    final = (await api.client.get(item_url(intent, item), headers=api.headers(user))).json()
    assert final["outcome"] == "no_bet" and final["results"] == [] and not final["retryable"]
    assert not (await snapshot(engine_app, como, user))["apostas"]


@pytest.mark.parametrize(
    "content,mime",
    [
        (b"", "image/png"),
        (b"not an image", "image/png"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        (b"GIF89a", "image/gif"),
    ],
)
async def test_invalid_empty_active_and_truncated_images_are_not_accepted(
    api, novo_usuario, content, mime
):
    user = await novo_usuario()
    intent, item, r = await submit(api, user, content, mime=mime)
    assert r.status_code == 422
    assert (
        await api.client.get(item_url(intent, item), headers=api.headers(user))
    ).status_code == 404


@pytest.mark.parametrize(
    "format_,mime",
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
async def test_real_raster_formats(api, novo_usuario, format_, mime):
    assert (await submit(api, await novo_usuario(), image_bytes(format_), mime=mime))[
        2
    ].status_code == 202


async def test_mime_conflict_and_real_byte_limit(api, novo_usuario):
    user = await novo_usuario()
    assert (await submit(api, user, image_bytes(), mime="image/jpeg"))[2].status_code == 422
    content = b"x" * (20 * 1024 * 1024 + 1)
    assert (await submit(api, user, content))[2].status_code == 413


async def test_twenty_items_limit_does_not_discard_previously_accepted(api, novo_usuario):
    user, intent, content = await novo_usuario(), uuid4(), image_bytes()
    for _ in range(20):
        assert (await submit(api, user, content, intent))[2].status_code == 202
    assert (await submit(api, user, content, intent))[2].status_code == 413
    state = await api.client.get(f"/api/v1/prints/intencoes/{intent}", headers=api.headers(user))
    assert state.json()["received_items"] == 20


async def test_daily_internal_limit_429_retry_after_and_replay_does_not_consume_slot(
    api, novo_usuario, monkeypatch
):
    from bancaemdia.config import get_settings

    monkeypatch.setattr(get_settings(), "PRINT_DAILY_ATTEMPT_LIMIT", 1)
    user = await novo_usuario()
    content = image_bytes()
    intent, item, r = await submit(api, user, content)
    assert r.status_code == 202
    assert (await submit(api, user, content, intent, item))[2].status_code == 202
    _, _, limited = await submit(api, user, image_bytes())
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0


async def test_missing_session_is_401_and_foreign_retry_is_404(api, novo_usuario):
    user, other = await novo_usuario(), await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    missing = await api.client.get(item_url(intent, item))
    assert missing.status_code == 401
    foreign = await api.client.post(
        item_url(intent, item) + "/retry",
        headers=api.headers(other),
        json={"retry_id": str(uuid4()), "expected_attempt": 1},
    )
    assert foreign.status_code == 404


async def test_read_only_keeps_reconciliation_and_denies_new_input_and_retry(
    api, engine_app, engine_admin, como, novo_usuario
):
    from bancaemdia.main import app

    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    async with engine_admin.connect() as conn:
        transaction = await conn.begin()
        await conn.execute(text("SELECT billing_activate_rollout()"))
        await conn.execute(text("SET LOCAL ROLE bancaemdia_app"))

        async def database():
            async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
                await session.execute(
                    text("SELECT set_config('app.current_user_id',:uid,true)"), {"uid": str(user)}
                )
                yield session

        old = {
            dependency: app.dependency_overrides[dependency]
            for dependency in (get_db, get_db_primary_snapshot)
        }
        app.dependency_overrides.update(dict.fromkeys(old, database))
        try:
            assert (
                await api.client.get(item_url(intent, item), headers=api.headers(user))
            ).status_code == 200
            assert (await submit(api, user, image_bytes()))[2].status_code == 402
            denied = await api.client.post(
                item_url(intent, item) + "/retry",
                headers=api.headers(user),
                json={"retry_id": str(uuid4()), "expected_attempt": 1},
            )
            assert denied.status_code == 402
        finally:
            app.dependency_overrides.update(old)
            await transaction.rollback()


async def test_retention_expires_private_bytes_and_state_but_preserves_tombstones(
    api, engine_app, como, novo_usuario, extractor
):
    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    identity = await asset_id(engine_app, como, user, intent, item)
    await worker.process_once(engine_app, user, identity)
    state = (await api.client.get(item_url(intent, item), headers=api.headers(user))).json()
    key = state["results"][0]["bet_key"]
    async with como(engine_app, user) as session:
        asset = await session.get(PrintAsset, identity)
        asset.media_expires_at = now() - timedelta(seconds=1)
        await session.commit()
    assert (await api.client.get(url(key), headers=api.headers(user))).status_code == 404
    async with como(engine_app, user) as session:
        intent_row = await session.get(models.PrintIntent, (user, intent))
        intent_row.expires_at = now() - timedelta(seconds=1)
        await session.commit()
    assert (
        await api.client.get(item_url(intent, item), headers=api.headers(user))
    ).status_code == 404
    assert (await submit(api, user, image_bytes(), intent, item))[2].status_code == 410


@pytest.fixture
def worker_broker():
    configured = os.environ.get("TEST_REDIS_URL")
    if configured:
        parsed = urlsplit(configured)
        yield urlunsplit((parsed.scheme, parsed.netloc, "/15", "", ""))
    else:
        from testcontainers.redis import RedisContainer

        with RedisContainer("redis:7-alpine") as container:
            yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/15"


async def test_real_worker_in_a_fresh_process_uses_isolated_extraction_adapter(
    api, engine_app, como, novo_usuario, worker_broker, tmp_path, monkeypatch
):
    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    identity = await asset_id(engine_app, como, user, intent, item)
    assert r.status_code == 202
    queue = "web-print-proof-" + uuid4().hex
    script = (
        "from bancaemdia.workers import web_print as w; w.extrair_bilhete=lambda *a,**k: "
        + repr(reading())
        + "; w.app.worker_main(['worker','--pool=solo','--concurrency=1','-Q',"
        + repr(queue)
        + ",'--without-gossip','--without-mingle','--without-heartbeat','--loglevel=WARNING'])"
    )
    env = {
        **os.environ,
        "DATABASE_URL": engine_app.url.render_as_string(hide_password=False),
        "PYTHONPATH": os.pathsep.join(sys.path),
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "REDIS_URL": worker_broker,
        "CELERY_BROKER_URL": worker_broker,
        "CELERY_RESULT_BACKEND": worker_broker,
    }
    env.pop("WORKER_METRICS_PORT", None)
    from celery import Celery

    log = Path(tmp_path) / "worker.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=output,
            stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        monkeypatch.setenv("CELERY_BROKER_URL", worker_broker)
        producer = Celery("web-print-proof", broker=worker_broker)
        try:
            await asyncio.to_thread(
                producer.send_task,
                "materialization.web_print",
                kwargs={"usuario_id": user, "asset_id": str(identity)},
                queue=queue,
                ignore_result=True,
            )
            for _ in range(45):
                assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")[
                    -2500:
                ]
                state = await api.client.get(item_url(intent, item), headers=api.headers(user))
                assert state.status_code == 200, state.text
                if state.json()["status"] == "completed":
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError(
                    "real Celery worker did not complete accepted file: "
                    + log.read_text(encoding="utf-8", errors="replace")[-2500:]
                )
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=15)
            producer.close()
            import redis

            redis.Redis.from_url(worker_broker).delete(queue, "_kombu.binding." + queue)


async def test_cookie_csrf_and_real_revocation(api, banco, engine_admin, tmp_path, monkeypatch):
    from pydantic import SecretStr
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from bancaemdia.auth import middleware, transport
    from bancaemdia.auth.identity_crypto import digest
    from bancaemdia.auth.identity_service import IdentityService
    from tests.identity.support import key_settings

    role, password = "print_auth_" + uuid4().hex, uuid4().hex
    async with engine_admin.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"))
        await conn.execute(text(f"GRANT bancaemdia_auth TO {role}"))
    dburl = (
        make_url(banco.url_admin)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(dburl, poolclass=NullPool, hide_parameters=True)
    settings = key_settings(
        tmp_path,
        AUTH_ENABLED=True,
        AUTH_DATABASE_URL=SecretStr(dburl),
        AUTH_PUBLIC_URL="https://api.test",
        AUTH_FRONTEND_ORIGIN="https://api.test",
        OIDC_ISSUER="https://issuer.test",
        OIDC_CLIENT_ID="fixture",
    )
    service = IdentityService(settings, engine)
    monkeypatch.setattr(middleware, "identity_settings", lambda: settings, raising=False)
    monkeypatch.setattr("bancaemdia.auth.identity_config.identity_settings", lambda: settings)
    monkeypatch.setattr("bancaemdia.auth.identity_service.identity_service", lambda: service)
    monkeypatch.setattr(transport, "identity_settings", lambda: settings)
    monkeypatch.setattr(transport, "identity_service", lambda: service)
    sid, cookie = str(uuid4()), uuid4().hex
    try:
        await service.verify_database_role()
        async with AsyncSession(engine) as session, session.begin():
            user, identity = await service.provision(
                session,
                {
                    "iss": settings.OIDC_ISSUER,
                    "sub": uuid4().hex,
                    "email_verified": True,
                    "email": uuid4().hex + "@test.invalid",
                    "name": "Print fixture",
                },
            )
            sealed = service._credentials(
                sid, user, 0, {"refresh_token": "synthetic", "id_token": "synthetic"}
            )
            await session.execute(
                text(
                    "INSERT INTO auth_private.sessions(id,identity_id,cookie_hash,encrypted,expires_at,idle_until,access_until) VALUES(:id,:identity,:cookie,:encrypted,now()+interval '1 hour',now()+interval '1 hour',now()+interval '5 minutes')"
                ),
                {"id": sid, "identity": identity, "cookie": digest(cookie), "encrypted": sealed},
            )
        intent, item = uuid4(), uuid4()
        address = item_url(intent, item)
        headers = {"Cookie": settings.session_cookie + "=" + cookie, "Origin": "https://api.test"}
        data = {"file": ("photo.png", image_bytes(), "image/png")}
        assert (await api.client.post(address, headers=headers, files=data)).status_code == 403
        headers["X-CSRF-Token"] = service.keys.csrf(cookie)
        accepted = await api.client.post(address, headers=headers, files=data)
        assert accepted.status_code == 202, accepted.text
        assert (
            await api.client.get(address, headers={"Cookie": headers["Cookie"]})
        ).status_code == 200
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE auth_private.sessions SET revoked_at=now() WHERE id=:id"), {"id": sid}
            )
        revoked = await api.client.get(address, headers={"Cookie": headers["Cookie"]})
        assert revoked.status_code == 401
        private(revoked)
    finally:
        await engine.dispose()
        async with engine_admin.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))


async def test_forced_rls_role_and_cross_owner_media(
    api, engine_admin, engine_app, como, novo_usuario, extractor
):
    user, other = await novo_usuario(), await novo_usuario()
    async with engine_admin.connect() as conn:
        role = (
            await conn.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='bancaemdia_app'")
            )
        ).one()
        assert role == (False, False)
        assert (
            list(
                (
                    await conn.execute(
                        text(
                            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('print_intents','print_items','print_assets')"
                        )
                    )
                ).all()
            )
            == [(True, True)] * 3
        )
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    identity = await asset_id(engine_app, como, user, intent, item)
    assert not await worker.process_once(engine_app, other, identity)
    await worker.process_once(engine_app, user, identity)
    state = (await api.client.get(item_url(intent, item), headers=api.headers(user))).json()
    key = state["results"][0]["bet_key"]
    assert (await api.client.get(url(key), headers=api.headers(other))).status_code == 404


async def test_malformed_multipart_and_missing_file_have_safe_codes(api, novo_usuario):
    user, address = await novo_usuario(), item_url(uuid4(), uuid4())
    headers = {**api.headers(user), "Content-Type": "multipart/form-data; boundary=synthetic"}
    malformed = await api.client.post(address, headers=headers, content=b"wrong boundary")
    assert malformed.status_code == 400
    missing = await api.client.post(
        address,
        headers=api.headers(user),
        files={"wrong": ("photo.png", image_bytes(), "image/png")},
    )
    assert missing.status_code == 422


async def test_extra_files_are_rejected_instead_of_silently_discarded(api, novo_usuario):
    user = await novo_usuario()
    address = item_url(uuid4(), uuid4())
    response = await api.client.post(
        address,
        headers=api.headers(user),
        files=[
            ("file", ("one.png", image_bytes(), "image/png")),
            ("file", ("two.png", image_bytes(), "image/png")),
        ],
    )
    assert response.status_code == 400


async def test_original_reconciliation_reads_do_not_change_any_financial_table(
    api, engine_app, como, novo_usuario, extractor
):
    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    await worker.process_once(
        engine_app, user, await asset_id(engine_app, como, user, intent, item)
    )
    before = await snapshot(engine_app, como, user)
    for _ in range(3):
        assert (
            await api.client.get(item_url(intent, item), headers=api.headers(user))
        ).status_code == 200
        assert (
            await api.client.get(f"/api/v1/prints/intencoes/{intent}", headers=api.headers(user))
        ).status_code == 200
    assert await snapshot(engine_app, como, user) == before


async def test_cached_reading_and_expired_lease_recover_without_paid_extraction(
    api, engine_app, como, novo_usuario, monkeypatch
):
    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    identity = await asset_id(engine_app, como, user, intent, item)
    async with como(engine_app, user) as session:
        asset = await session.get(PrintAsset, identity)
        asset.status = "processing"
        asset.lease_token = uuid4()
        asset.lease_until = now() - timedelta(seconds=1)
        asset.reading = reading()
        await session.commit()

    def forbidden(*a, **kw):
        raise AssertionError("cached recovery must not call paid extraction")

    monkeypatch.setattr(worker, "extrair_bilhete", forbidden)
    assert await worker.process_once(engine_app, user, identity)
    state = (await api.client.get(item_url(intent, item), headers=api.headers(user))).json()
    assert state["status"] == "completed" and state["attempt"] == 2


async def test_duplicate_failed_alias_can_retry_once_without_new_financial_identity(
    api, engine_app, como, novo_usuario, monkeypatch
):
    user, content = await novo_usuario(), image_bytes()
    intent, item, r = await submit(api, user, content)
    identity = await asset_id(engine_app, como, user, intent, item)

    def unavailable(*a, **kw):
        raise TimeoutError("synthetic")

    monkeypatch.setattr(worker, "extrair_bilhete", unavailable)
    await worker.process_once(engine_app, user, identity)
    other_intent, alias, r = await submit(api, user, content)
    assert r.status_code == 202 and r.json()["duplicate"] and r.json()["retryable"]
    retry_id = str(uuid4())
    retried = await api.client.post(
        item_url(other_intent, alias) + "/retry",
        headers=api.headers(user),
        json={"retry_id": retry_id, "expected_attempt": 1},
    )
    assert retried.status_code == 202 and retried.json()["last_retry_id"] == retry_id
    monkeypatch.setattr(worker, "extrair_bilhete", lambda *a, **kw: reading())
    await worker.process_once(engine_app, user, identity)
    original = (await api.client.get(item_url(intent, item), headers=api.headers(user))).json()
    duplicate = (
        await api.client.get(item_url(other_intent, alias), headers=api.headers(user))
    ).json()
    assert original["results"] == duplicate["results"]
    assert len((await snapshot(engine_app, como, user))["apostas"]) == 1


async def test_internal_cost_and_failure_text_never_enter_public_review(
    api, engine_app, como, novo_usuario, monkeypatch
):
    value = reading(stake=None)
    value.update({
        "custo_usd": 999.123,
        "estimated_cost_usd": 999.123,
        "motivo": "a leitura falhou (US$ 999.123)",
        "provider_secret": "synthetic-private-provider",
    })
    monkeypatch.setattr(worker, "extrair_bilhete", lambda *a, **kw: value)
    user = await novo_usuario()
    intent, item, r = await submit(api, user, image_bytes())
    assert r.status_code == 202
    await worker.process_once(
        engine_app, user, await asset_id(engine_app, como, user, intent, item)
    )
    status = await api.client.get(item_url(intent, item), headers=api.headers(user))
    revision = status.json()["results"][0]["review_ids"][0]
    detail = await api.client.get(f"/api/v1/revisao/{revision}", headers=api.headers(user))
    assert all(
        token not in detail.text and token not in status.text
        for token in ("custo_usd", "estimated_cost_usd", "provider_secret", "999.123")
    )


async def test_real_raster_dimension_budget_is_enforced_on_intake(api, novo_usuario):
    import struct
    import zlib

    content = bytearray(image_bytes())
    content[16:24] = struct.pack(">II", 8000, 6000)
    content[29:33] = struct.pack(">I", zlib.crc32(content[12:29]))
    response = (await submit(api, await novo_usuario(), bytes(content)))[2]
    assert response.status_code == 422


async def test_database_dependency_503_is_safe_and_has_retry_after(api, novo_usuario, monkeypatch):
    from sqlalchemy.exc import OperationalError as SqlOperationalError

    def unavailable(*a, **kw):
        raise SqlOperationalError("synthetic private SQL", {}, Exception("private dependency"))

    monkeypatch.setattr("bancaemdia.api.v1.web_print.find", unavailable)
    result = await api.client.get(
        item_url(uuid4(), uuid4()), headers=api.headers(await novo_usuario())
    )
    assert result.status_code == 503 and result.headers["Retry-After"] == "5"
    assert "private" not in result.text and "SQL" not in result.text
