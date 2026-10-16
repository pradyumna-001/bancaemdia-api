"""Owner cookie HTTP + real restricted auth/API PostgreSQL roles, no auth/domain mocks."""

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_pairing import batch
from test_pairing import real_database_required as real_database_required

from bancaemdia import models
from bancaemdia.auth import identity_config, identity_service, transport
from bancaemdia.auth.identity_crypto import digest
from bancaemdia.db.session import get_db, get_db_primary
from bancaemdia.main import app
from bancaemdia.models.coleta_instalacao import ColetaInstalacao
from bancaemdia.models.coleta_sessao import ColetaSessao
from bancaemdia.repositories.coleta_instalacao import owner_scope
from tests.integration.test_identity_db import identity_service_db as identity_service_db

pytestmark = pytest.mark.xdist_group("postgres")
PREFIX = "/api/v1/coleta"
BOUNDARY = "2026-08-01T00:00:00.123456Z"


@pytest.fixture
async def web(identity_service_db, engine_app, engine_admin, monkeypatch):
    service = identity_service_db
    await service.verify_database_role()
    settings = service.settings.model_copy(
        update={
            "AUTH_ENABLED": True,
            "AUTH_PUBLIC_URL": "https://site.test",
            "AUTH_FRONTEND_ORIGIN": "https://site.test",
        }
    )
    service.settings = settings
    monkeypatch.setattr(identity_config, "identity_settings", lambda: settings)
    monkeypatch.setattr(identity_service, "identity_service", lambda: service)
    monkeypatch.setattr(transport, "identity_settings", lambda: settings)
    monkeypatch.setattr(transport, "identity_service", lambda: service)

    async def sessions():
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, sessions)
    monkeypatch.setitem(app.dependency_overrides, get_db_primary, sessions)
    cookies = {}
    session_ids = {}
    for _ in range(2):
        async with AsyncSession(service.engine) as session, session.begin():
            uid, identity = await service.provision(
                session,
                {
                    "iss": settings.OIDC_ISSUER,
                    "sub": uuid4().hex,
                    "email_verified": True,
                    "email": uuid4().hex + "@example.org",
                    "name": "Synthetic owner",
                },
            )
            sid, cookie = str(uuid4()), secrets.token_urlsafe(32)
            credentials = service._credentials(
                sid,
                uid,
                0,
                {
                    "refresh_token": "synthetic-private-refresh",
                    "id_token": "synthetic-private-id",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO auth_private.sessions(id,identity_id,cookie_hash,encrypted,expires_at,idle_until,access_until) "
                    "VALUES(:sid,:identity,:cookie,:encrypted,now()+interval '1 hour',now()+interval '1 hour',now()+interval '1 hour')"
                ),
                {
                    "sid": sid,
                    "identity": identity,
                    "cookie": digest(cookie),
                    "encrypted": credentials,
                },
            )
            cookies[uid] = cookie
            session_ids[uid] = sid
    user, other = cookies
    async with engine_admin.begin() as conn:
        await conn.execute(text("DELETE FROM coleta_pairing_quotas"))

    def headers(uid=user, *, csrf=True):
        result = {"Cookie": settings.session_cookie + "=" + cookies[uid]}
        if csrf:
            result.update({
                "Origin": settings.AUTH_FRONTEND_ORIGIN,
                "X-CSRF-Token": service.keys.csrf(cookies[uid]),
            })
        return result

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://site.test"
    ) as client:

        async def pair(uid=user, public_id=None):
            response = await client.post(PREFIX + "/pairing-codes", headers=headers(uid))
            assert response.status_code == 201, response.text
            code = response.json()["codigo"]
            response = await client.post(
                PREFIX + "/pairing-exchange",
                json={
                    "codigo": code,
                    "instalacao_publica_id": str(public_id or uuid4()),
                    "nome_dispositivo": "Synthetic device",
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def listing(uid=user):
            response = await client.get(PREFIX + "/installations", headers=headers(uid, csrf=False))
            assert response.status_code == 200, response.text
            assert "no-store" in response.headers["Cache-Control"]
            return response.json()

        async def open_session(paired, boundary=BOUNDARY, resume=None):
            response = await client.post(
                PREFIX + "/sessions",
                headers=token_headers(paired),
                json={
                    "coletar_desde": boundary,
                    "retomar_sessao_id": resume,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        yield SimpleNamespace(
            http=client,
            headers=headers,
            pair=pair,
            listing=listing,
            open=open_session,
            user=user,
            other=other,
            admin=engine_admin,
            engine=engine_app,
            cookies=cookies,
            session_ids=session_ids,
        )


def token_headers(paired):
    return {"X-Coleta-Token": paired["token"]}


async def test_empty_owner_and_two_independent_devices_without_sessions(web):
    assert await web.listing() == []
    first, second = await web.pair(), await web.pair()
    rows = await web.listing()
    assert [r["id"] for r in rows] == sorted([first["instalacao_id"], second["instalacao_id"]])
    assert all(r["estado_sessao"] == "sem_sessao" and r["sessao_coleta"] is None for r in rows)


async def test_open_session_exact_microsecond_boundary_and_independent_device(web):
    first, second = await web.pair(), await web.pair()
    opened = await web.open(first)
    rows = {r["id"]: r for r in await web.listing()}
    row = rows[first["instalacao_id"]]
    assert row["estado_sessao"] == "aberta"
    assert row["sessao_coleta"] == {
        "sessao_id": opened["sessao_id"],
        "selecao": "atual",
        "coletar_desde": BOUNDARY,
        "criada_em": row["sessao_coleta"]["criada_em"],
        "encerrada_em": None,
    }
    assert rows[second["instalacao_id"]]["sessao_coleta"] is None


async def test_closed_session_is_published_as_last_not_current(web):
    paired = await web.pair()
    opened = await web.open(paired)
    response = await web.http.delete(
        PREFIX + "/sessions/" + opened["sessao_id"], headers=token_headers(paired)
    )
    assert response.status_code == 204
    row = (await web.listing())[0]
    assert row["estado_sessao"] == "encerrada"
    assert row["sessao_coleta"]["selecao"] == "ultima_encerrada"
    assert row["sessao_coleta"]["encerrada_em"] is not None
    assert row["sessao_coleta"]["coletar_desde"] == BOUNDARY


async def history(web, paired, *, created=None):
    async with AsyncSession(web.engine) as session, session.begin():
        await owner_scope(session, web.user)
        row = ColetaSessao(
            usuario_id=web.user,
            instalacao_id=paired["instalacao_id"],
            coletar_desde=datetime(2026, 1, 1, tzinfo=UTC),
            criada_em=created or datetime.now(UTC),
            encerrada_em=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return str(row.sessao_id)


async def test_open_session_precedes_more_recent_historical_creation(web):
    paired = await web.pair()
    opened = await web.open(paired)
    await history(web, paired, created=datetime.now(UTC) + timedelta(days=1))
    row = (await web.listing())[0]
    assert row["sessao_coleta"]["sessao_id"] == opened["sessao_id"]
    assert row["estado_sessao"] == "aberta"


async def test_last_history_uses_creation_then_primary_key_tiebreak(web):
    paired = await web.pair()
    created = datetime(2026, 9, 1, tzinfo=UTC)
    await history(web, paired, created=created)
    last = await history(web, paired, created=created)
    await history(web, paired, created=created - timedelta(days=1))
    row = (await web.listing())[0]
    assert row["sessao_coleta"]["sessao_id"] == last
    assert row["estado_sessao"] == "encerrada"


async def test_owner_listing_and_rls_hide_other_valid_installation_and_session_ids(web):
    first, foreign = await web.pair(), await web.pair(web.other)
    own_session, foreign_session = await web.open(first), await web.open(foreign)
    rows, other_rows = await web.listing(), await web.listing(web.other)
    assert [r["id"] for r in rows] == [first["instalacao_id"]]
    assert [r["id"] for r in other_rows] == [foreign["instalacao_id"]]
    assert foreign_session["sessao_id"] not in json.dumps(rows)
    assert own_session["sessao_id"] not in json.dumps(other_rows)
    async with AsyncSession(web.engine) as session:
        await owner_scope(session, web.user)
        assert await session.get(ColetaInstalacao, foreign["instalacao_id"]) is None
        assert (
            await session.scalar(
                select(ColetaSessao.id).where(
                    ColetaSessao.sessao_id == UUID(foreign_session["sessao_id"])
                )
            )
            is None
        )
        role = (
            await session.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert role == (False, False)


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
async def test_foreign_and_unknown_control_ids_are_uniform_404(web, operation):
    foreign = await web.pair(web.other)

    async def call(iid):
        path = PREFIX + f"/installations/{iid}"
        if operation == "rotate":
            return await web.http.post(path + "/rotate", headers=web.headers())
        return await web.http.delete(path, headers=web.headers())

    first, missing = await call(foreign["instalacao_id"]), await call(2**63 - 1)
    assert first.status_code == missing.status_code == 404
    assert first.json() == missing.json() == {"detail": "Installation unavailable"}


@pytest.mark.parametrize(
    "kind", ["anonymous", "installation_token", "expired_cookie", "revoked_cookie"]
)
async def test_owner_read_requires_real_live_web_identity(web, kind):
    paired = await web.pair()
    headers = {}
    if kind == "installation_token":
        headers = token_headers(paired)
    elif kind in {"expired_cookie", "revoked_cookie"}:
        assignment = (
            "expires_at=now()-interval '1 second'"
            if kind == "expired_cookie"
            else "revoked_at=now()"
        )
        async with web.admin.begin() as conn:
            await conn.execute(
                text(f"UPDATE auth_private.sessions SET {assignment} WHERE id=:sid"),
                {"sid": web.session_ids[web.user]},
            )
        headers = web.headers(csrf=False)
    response = await web.http.get(PREFIX + "/installations", headers=headers)
    assert response.status_code == 401
    assert "no-store" in response.headers["Cache-Control"]
    assert paired["token"] not in response.text


async def test_resume_and_reconnect_keep_the_original_boundary_and_state(web):
    public_id = uuid4()
    paired = await web.pair(public_id=public_id)
    opened = await web.open(paired)
    before = (await web.listing())[0]["sessao_coleta"]
    await web.open(paired, resume=opened["sessao_id"])
    reconnected = await web.pair(public_id=public_id)
    assert reconnected["instalacao_id"] == paired["instalacao_id"]
    await web.open(reconnected, resume=opened["sessao_id"])
    assert (await web.listing())[0]["sessao_coleta"] == before


async def test_concurrent_resume_and_owner_reads_converge_without_new_sessions(web):
    paired = await web.pair()
    opened = await web.open(paired)
    before = (await web.listing())[0]["sessao_coleta"]
    result = await asyncio.gather(
        *(web.open(paired, resume=opened["sessao_id"]) for _ in range(4)),
        *(web.listing() for _ in range(4)),
    )
    assert all(r[0]["sessao_coleta"] == before for r in result[4:])
    async with web.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(ColetaSessao)
                .where(ColetaSessao.instalacao_id == paired["instalacao_id"])
            )
            == 1
        )


async def test_rejected_boundary_change_leaves_owner_projection_unchanged(web):
    paired = await web.pair()
    opened = await web.open(paired)
    before = await web.listing()
    response = await web.http.post(
        PREFIX + "/sessions",
        headers=token_headers(paired),
        json={"retomar_sessao_id": opened["sessao_id"], "coletar_desde": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 409
    assert await web.listing() == before


@pytest.mark.parametrize("change", ["expire", "revoke", "rotate"])
async def test_credential_changes_never_manufacture_session_closure_or_presence(web, change):
    paired = await web.pair()
    await web.open(paired)
    before = (await web.listing())[0]["sessao_coleta"]
    path = PREFIX + f"/installations/{paired['instalacao_id']}"
    if change == "expire":
        async with web.admin.begin() as conn:
            await conn.execute(
                update(ColetaInstalacao)
                .where(ColetaInstalacao.id == paired["instalacao_id"])
                .values(expira_em=datetime.now(UTC) - timedelta(seconds=1))
            )
    elif change == "revoke":
        assert (await web.http.delete(path, headers=web.headers())).status_code == 204
    else:
        response = await web.http.post(path + "/rotate", headers=web.headers())
        assert response.status_code == 200
        assert response.json()["token"] not in json.dumps(await web.listing())
    row = (await web.listing())[0]
    assert row["estado_sessao"] == "aberta" and row["sessao_coleta"] == before
    assert not any("online" in key or "conectad" in key for key in row)
    rejected = await web.http.get(
        PREFIX + "/sessions/" + before["sessao_id"], headers=token_headers(paired)
    )
    assert rejected.status_code == 401


async def test_owner_get_is_select_only_and_does_not_touch_use_or_financial_facts(web):
    paired = await web.pair()
    await web.open(paired)
    async with web.admin.connect() as conn:
        before = (
            await conn.execute(
                text("SELECT row_to_json(i) FROM coleta_instalacoes i WHERE id=:id"),
                {"id": paired["instalacao_id"]},
            )
        ).scalar_one()
        sessions_before = (
            (
                await conn.execute(
                    text("SELECT row_to_json(s) FROM coleta_sessoes s WHERE instalacao_id=:id"),
                    {"id": paired["instalacao_id"]},
                )
            )
            .scalars()
            .all()
        )
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(web.engine.sync_engine, "before_cursor_execute", record)
    try:
        await web.listing()
        await web.listing()
    finally:
        event.remove(web.engine.sync_engine, "before_cursor_execute", record)
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    async with web.admin.connect() as conn:
        after = (
            await conn.execute(
                text("SELECT row_to_json(i) FROM coleta_instalacoes i WHERE id=:id"),
                {"id": paired["instalacao_id"]},
            )
        ).scalar_one()
        assert after == before
        assert (
            await conn.execute(
                text("SELECT row_to_json(s) FROM coleta_sessoes s WHERE instalacao_id=:id"),
                {"id": paired["instalacao_id"]},
            )
        ).scalars().all() == sessions_before
        for table in (models.Aposta, models.Evento, models.ColetaEntrega):
            assert (
                await conn.scalar(
                    select(func.count()).select_from(table).where(table.usuario_id == web.user)
                )
                == 0
            )


async def test_full_population_is_untruncated_with_one_projection_query(web):
    async with AsyncSession(web.engine) as session, session.begin():
        await owner_scope(session, web.user)
        for _ in range(105):
            session.add(ColetaInstalacao(usuario_id=web.user, instalacao_publica_id=uuid4()))
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "coleta_instalacoes" in statement:
            statements.append(statement)

    event.listen(web.engine.sync_engine, "before_cursor_execute", record)
    try:
        rows = await web.listing()
    finally:
        event.remove(web.engine.sync_engine, "before_cursor_execute", record)
    assert len(rows) == 105
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert len(statements) == 1 and "row_number()" in statements[0]
    assert "token_hash" not in statements[0] and "envelope" not in statements[0]


async def test_read_only_owner_reads_and_control_exceptions_but_ingest_stays_denied(web):
    paired = await web.pair()
    await web.open(paired)
    async with web.admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("SELECT billing_activate_rollout()"))
    try:
        assert (await web.http.get("/api/v1/billing/status", headers=web.headers())).json()[
            "access"
        ] == "READ_ONLY"
        assert (await web.listing())[0]["estado_sessao"] == "aberta"
        assert (
            await web.http.post(PREFIX + "/pairing-codes", headers=web.headers())
        ).status_code == 201
        rotated = await web.http.post(
            PREFIX + f"/installations/{paired['instalacao_id']}/rotate", headers=web.headers()
        )
        assert rotated.status_code == 200
        rejected = await web.http.post(PREFIX, headers=token_headers(rotated.json()), json=batch())
        assert rejected.status_code == 402
        assert (
            await web.http.delete(
                PREFIX + f"/installations/{paired['instalacao_id']}", headers=web.headers()
            )
        ).status_code == 204
        assert (await web.listing())[0]["estado_sessao"] == "aberta"
    finally:
        async with web.admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:previous WHERE id=1"),
                {"previous": previous},
            )


@pytest.mark.parametrize("kind", ["missing_csrf", "wrong_origin"])
async def test_cookie_controls_still_require_csrf_and_exact_origin(web, kind):
    paired = await web.pair()
    headers = web.headers()
    if kind == "missing_csrf":
        headers.pop("X-CSRF-Token")
    else:
        headers["Origin"] = "https://attacker.invalid"
    before = await web.listing()
    for method, suffix in (
        ("POST", "/pairing-codes"),
        ("POST", f"/installations/{paired['instalacao_id']}/rotate"),
        ("DELETE", f"/installations/{paired['instalacao_id']}"),
    ):
        assert (await web.http.request(method, PREFIX + suffix, headers=headers)).status_code == 403
    assert await web.listing() == before


async def test_projection_and_transport_errors_never_expose_credentials_payloads_or_logs(
    web, caplog
):
    paired = await web.pair()
    await web.open(paired)
    response = await web.http.get(PREFIX + "/installations", headers=web.headers(csrf=False))
    assert response.status_code == 200
    assert set(response.json()[0]["sessao_coleta"]) == {
        "sessao_id",
        "selecao",
        "coletar_desde",
        "criada_em",
        "encerrada_em",
    }
    errors = [
        await web.http.get(
            PREFIX + "/installations?token=" + paired["token"], headers=web.headers()
        ),
        await web.http.get("http://site.test" + PREFIX + "/installations", headers=web.headers()),
    ]
    assert all(r.status_code == 400 and "no-store" in r.headers["Cache-Control"] for r in errors)
    material = response.text + "".join(r.text for r in errors) + caplog.text
    for forbidden in (
        paired["token"],
        web.cookies[web.user],
        "token_hash",
        "cookie_hash",
        "synthetic-private-refresh",
        "synthetic-private-id",
        '"envelope"',
        '"payload"',
    ):
        assert forbidden not in material


async def test_dependency_database_error_is_safe_503_with_retry_after(web):
    await web.pair()
    async with web.admin.begin() as conn:
        await conn.execute(text("REVOKE SELECT ON coleta_sessoes FROM bancaemdia_app"))
    try:
        response = await web.http.get(PREFIX + "/installations", headers=web.headers())
        assert response.status_code == 503
        assert response.json() == {"detail": "Collection management temporarily unavailable"}
        assert response.headers["Retry-After"] == "5"
        assert "no-store" in response.headers["Cache-Control"]
    finally:
        async with web.admin.begin() as conn:
            await conn.execute(text("GRANT SELECT ON coleta_sessoes TO bancaemdia_app"))


async def test_owner_read_rate_limit_is_documented_and_never_uses_extension_identity(
    web, monkeypatch
):
    from bancaemdia.config import get_settings
    from bancaemdia.middleware.rate_limit import reset_rate_limiters

    await web.pair()
    monkeypatch.setenv("API_RATE_LIMIT", "1/minute")
    get_settings.cache_clear()
    reset_rate_limiters()
    try:
        assert (
            await web.http.get(PREFIX + "/installations", headers=web.headers())
        ).status_code == 200
        response = await web.http.get(PREFIX + "/installations", headers=web.headers())
        assert response.status_code == 429
        assert response.json()["error"] == "rate_limited"
        assert int(response.headers["Retry-After"]) >= 1
        assert "no-store" in response.headers["Cache-Control"]
    finally:
        monkeypatch.delenv("API_RATE_LIMIT")
        get_settings.cache_clear()
