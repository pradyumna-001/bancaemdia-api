"""Real HTTP/JWT/restricted PostgreSQL, including concurrency and legacy migration."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_public_key,
)
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bancaemdia import models
from bancaemdia.api.v1 import coleta
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db, get_db_primary
from bancaemdia.main import app
from bancaemdia.models.coleta_instalacao import ColetaInstalacao, ColetaPairingCode
from bancaemdia.repositories.coleta_instalacao import ColetaInstalacaoRepo, owner_scope
from bancaemdia.services import coleta_tokens

pytestmark = pytest.mark.xdist_group("postgres")
PREFIX = "/api/v1/coleta"


@pytest.fixture(scope="module", autouse=True)
def real_database_required():
    if not os.environ.get("TEST_DATABASE_URL"):
        from testcontainers.core.docker_client import DockerClient

        assert DockerClient().client.ping(), "Pairing requires real PostgreSQL"


@pytest.fixture(scope="module")
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return private, {
        **jwt.algorithms.RSAAlgorithm.to_jwk(load_pem_public_key(public), as_dict=True),
        "kid": "pairing-test",
    }


@pytest.fixture
async def system(engine_app, engine_admin, novo_usuario, monkeypatch, signing_key):
    user, other = await novo_usuario(), await novo_usuario()
    cache = JWKSCache(None, "RS256")
    cache.keys = {"pairing-test": signing_key[1]}
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)

    async def sessions():
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, sessions)
    monkeypatch.setitem(app.dependency_overrides, get_db_primary, sessions)
    queued = []
    monkeypatch.setattr(coleta, "_enfileirar", lambda uid, rows: queued.append((uid, rows)))
    async with engine_admin.begin() as connection:
        await connection.execute(text("DELETE FROM coleta_pairing_quotas"))
        await connection.execute(insert(models.Casa).values(nome="Betano").on_conflict_do_nothing())

    def headers(uid=user):
        settings = get_settings()
        token = jwt.encode(
            {
                "sub": str(uid),
                "aud": settings.JWT_AUDIENCE,
                "iss": settings.JWT_ISSUER,
                "exp": int(time.time()) + 300,
            },
            signing_key[0],
            algorithm="RS256",
            headers={"kid": "pairing-test"},
        )
        return {"Authorization": "Bearer " + token}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(f"192.0.2.{user % 250 + 1}", 1234)),
        base_url="https://api.test",
    ) as client:

        async def code(uid=user):
            response = await client.post(PREFIX + "/pairing-codes", headers=headers(uid))
            assert response.status_code == 201, response.text
            assert response.headers["Cache-Control"] == "no-store"
            return response.json()["codigo"]

        async def pair(uid=user, public_id=None):
            challenge = await code(uid)
            public_id = public_id or uuid4()
            response = await client.post(
                PREFIX + "/pairing-exchange",
                json={
                    "codigo": challenge,
                    "instalacao_publica_id": str(public_id),
                    "nome_dispositivo": "Dispositivo sintético",
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        yield SimpleNamespace(
            http=client,
            code=code,
            pair=pair,
            headers=headers,
            user=user,
            other=other,
            engine=engine_app,
            admin=engine_admin,
            queued=queued,
        )


def token_headers(pair):
    return {"X-Coleta-Token": pair["token"]}


def batch():
    return {
        "contrato": 1,
        "casa": "betano",
        "apostas": [
            {
                "id": uuid4().hex,
                "bonusType": 0,
                "totalAmount": 160.0,
                "totalAmountWithCurrency": {"amount": 160.0, "currencyCode": "BRL"},
                "totalOdds": 1.90,
                "finalWinnings": 0,
                "placedAt": 1785708161930,
                "finalBetResult": "Lose",
                "settledAt": 1785711761930,
                "legs": [
                    {
                        "legItems": [
                            {
                                "eventId": "86389413",
                                "eventName": "Azul - Verde",
                                "startTime": 1785709800000,
                                "selections": [{"description": "Mais de 41.5", "odds": 1.90}],
                            }
                        ]
                    }
                ],
            }
        ],
    }


async def test_two_installations_rotate_revoke_and_reconnect_independently(system):
    first_id = uuid4()
    first, second = await system.pair(public_id=first_id), await system.pair()
    for credential in (first, second):
        response = await system.http.post(PREFIX, headers=token_headers(credential), json=batch())
        assert response.status_code == 200, response.text
        assert response.json()["novas_contando"] == 1
        status = await system.http.get(PREFIX + "/status", headers=token_headers(credential))
        assert status.json() == {
            "usuario_id": system.user,
            "instalacao_id": credential["instalacao_id"],
        }
    assert len(system.queued) == 2 and all(
        uid == system.user and len(rows) == 1 for uid, rows in system.queued
    )
    rotated = await system.http.post(
        PREFIX + f"/installations/{first['instalacao_id']}/rotate", headers=system.headers()
    )
    assert rotated.status_code == 200
    assert (
        await system.http.post(PREFIX, json=batch(), headers=token_headers(first))
    ).status_code == 403
    assert (
        await system.http.get(PREFIX + "/status", headers=token_headers(rotated.json()))
    ).status_code == 200
    assert (
        await system.http.delete(
            PREFIX + f"/installations/{second['instalacao_id']}", headers=system.headers()
        )
    ).status_code == 204
    assert (
        await system.http.post(PREFIX, json=batch(), headers=token_headers(second))
    ).status_code == 403
    assert (
        await system.http.get(PREFIX + "/status", headers=token_headers(rotated.json()))
    ).status_code == 200
    # Re-pairing the same client identity changes its secret, retaining a single installation.
    assert (
        await system.http.delete(
            PREFIX + f"/installations/{first['instalacao_id']}", headers=system.headers()
        )
    ).status_code == 204
    reconnected = await system.pair(public_id=first_id)
    assert reconnected["instalacao_id"] == first["instalacao_id"]
    assert (
        await system.http.get(PREFIX + "/status", headers=token_headers(rotated.json()))
    ).status_code == 403
    listing = await system.http.get(PREFIX + "/installations", headers=system.headers())
    assert len(listing.json()) == 2 and "token_hash" not in listing.text
    assert first["token"] not in listing.text and second["token"] not in listing.text
    assert any(row["revogado_em"] for row in listing.json())
    assert any(row["ultimo_uso_em"] and row["rotacionado_em"] for row in listing.json())


@pytest.mark.parametrize("kind", ["wrong", "expired", "reused", "inactive"])
async def test_unavailable_codes_are_neutral_and_never_issue_a_second_credential(system, kind):
    code = await system.code()
    if kind == "expired":
        async with system.admin.begin() as conn:
            await conn.execute(
                update(ColetaPairingCode)
                .where(ColetaPairingCode.code_hash == coleta_tokens.digest(code, "pairing"))
                .values(
                    criado_em=datetime.now(UTC) - timedelta(minutes=20),
                    expira_em=datetime.now(UTC) - timedelta(minutes=10),
                )
            )
    elif kind == "inactive":
        async with system.admin.begin() as conn:
            await conn.execute(
                update(models.Usuario).where(models.Usuario.id == system.user).values(ativo=False)
            )
    elif kind == "reused":
        accepted = await system.http.post(
            PREFIX + "/pairing-exchange",
            json={"codigo": code, "instalacao_publica_id": str(uuid4())},
        )
        assert accepted.status_code == 200
    else:
        code = "cpc_synthetic_wrong"
    response = await system.http.post(
        PREFIX + "/pairing-exchange", json={"codigo": code, "instalacao_publica_id": str(uuid4())}
    )
    assert response.status_code == 400 and response.json() == {
        "detail": "Invalid or unavailable pairing code"
    }


async def test_concurrent_exchange_consumes_one_code_once(system):
    code = await system.code()
    responses = await asyncio.gather(*[
        system.http.post(
            PREFIX + "/pairing-exchange",
            json={"codigo": code, "instalacao_publica_id": str(uuid4())},
        )
        for _ in range(10)
    ])
    assert sorted(response.status_code for response in responses) == [200] + [400] * 9
    async with system.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(ColetaInstalacao)
                .where(ColetaInstalacao.usuario_id == system.user)
            )
            == 1
        )
        assert await conn.scalar(
            select(ColetaPairingCode.consumido_em).where(
                ColetaPairingCode.code_hash == coleta_tokens.digest(code, "pairing")
            )
        )


async def test_cross_tenant_rls_and_least_privilege(system):
    same_public_id = uuid4()
    first, other = (
        await system.pair(public_id=same_public_id),
        await system.pair(system.other, same_public_id),
    )
    assert first["instalacao_id"] != other["instalacao_id"]
    for method, path in (
        ("post", f"/installations/{other['instalacao_id']}/rotate"),
        ("delete", f"/installations/{other['instalacao_id']}"),
    ):
        response = await system.http.request(method, PREFIX + path, headers=system.headers())
        assert response.status_code == 404
    for path in (PREFIX + "/installations", "/api/v1/apostas"):
        assert (await system.http.get(path, headers=token_headers(first))).status_code == 401
    assert (
        await system.http.post(PREFIX + "/pairing-codes", headers=token_headers(first))
    ).status_code == 401
    for uid in (None, system.user):
        async with AsyncSession(system.engine) as session:
            if uid:
                await owner_scope(session, uid)
            assert await session.get(ColetaInstalacao, other["instalacao_id"]) is None
            statement = (
                update(ColetaInstalacao)
                .where(ColetaInstalacao.id == other["instalacao_id"])
                .values(nome_dispositivo="intrusão")
            )
            assert (await session.execute(statement)).rowcount == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ColetaPairingCode)
                    .where(ColetaPairingCode.usuario_id == system.other)
                )
                == 0
            )
    status = await system.http.get(
        PREFIX + "/status", headers={**token_headers(other), **system.headers()}
    )
    assert (
        status.json()["usuario_id"] == system.other
    )  # Collection identity comes only from its credential.


async def test_account_anonymization_removes_owned_credentials_and_preserves_other_installation(
    system,
):
    paired = await system.pair()
    other = await system.pair(system.other)
    unused_code = await system.code()
    response = await system.http.delete("/api/v1/usuario/me", headers=system.headers())
    assert response.status_code == 200, response.text
    async with system.admin.connect() as conn:
        for table in (ColetaInstalacao, ColetaPairingCode):
            assert (
                await conn.scalar(
                    select(func.count()).select_from(table).where(table.usuario_id == system.user)
                )
                == 0
            )
    assert (
        await system.http.get(PREFIX + "/status", headers=token_headers(paired))
    ).status_code == 403
    assert (
        await system.http.get(PREFIX + "/status", headers=token_headers(other))
    ).status_code == 200
    replay = await system.http.post(
        PREFIX + "/pairing-exchange",
        json={"codigo": unused_code, "instalacao_publica_id": str(uuid4())},
    )
    assert replay.status_code == 400


@pytest.mark.parametrize("action", ["create", "exchange"])
async def test_shared_quota_concurrency_expiry_and_isolation(system, monkeypatch, action):
    settings = get_settings().model_copy(
        update={"COLETA_PAIRING_CREATE_LIMIT": 2, "COLETA_PAIRING_EXCHANGE_LIMIT": 2}
    )
    monkeypatch.setattr(coleta_tokens, "get_settings", lambda: settings)

    async def attempt(identity):
        async with AsyncSession(system.engine) as session:
            try:
                await coleta_tokens.admit(session, action=action, identity=identity)
                return True
            except Exception as error:
                assert error.status_code == 429
                return False

    assert sum(await asyncio.gather(*[attempt("same") for _ in range(10)])) == 2
    assert await attempt("other")
    async with system.admin.begin() as conn:
        await conn.execute(
            text("UPDATE coleta_pairing_quotas SET expires_at=now()-interval '1 second'")
        )
    assert await attempt("same")


async def test_quota_outage_never_fails_open(system, monkeypatch):
    original = AsyncSession.scalar

    async def unavailable(session, statement, *args, **kwargs):
        if "coleta_pairing_limit" in str(statement):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("SENTINEL-code", {}, Exception("SENTINEL-token"))
        return await original(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", unavailable)
    response = await system.http.post(PREFIX + "/pairing-codes", headers=system.headers())
    assert response.status_code == 503 and "SENTINEL" not in response.text
    async with system.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(ColetaPairingCode)
                .where(ColetaPairingCode.usuario_id == system.user)
            )
            == 0
        )


@pytest.mark.parametrize("revoke", [False, True])
async def test_rotation_waits_for_inflight_authenticated_transaction(system, revoke):
    paired = await system.pair()
    async with AsyncSession(system.engine) as session:
        identity = await ColetaInstalacaoRepo().authenticate(
            session, coleta_tokens.digest(paired["token"])
        )
        assert identity.usuario_id == system.user
        rotating = asyncio.create_task(
            system.http.request(
                "DELETE" if revoke else "POST",
                PREFIX
                + f"/installations/{paired['instalacao_id']}"
                + ("" if revoke else "/rotate"),
                headers=system.headers(),
            )
        )
        await asyncio.sleep(0.15)
        assert not rotating.done()
        await session.commit()
        response = await rotating
    assert response.status_code == (204 if revoke else 200)
    assert (
        await system.http.get(PREFIX + "/status", headers=token_headers(paired))
    ).status_code == 403


async def test_only_hashes_and_minimal_identity_persist(system):
    code = await system.code()
    response = await system.http.post(
        PREFIX + "/pairing-exchange", json={"codigo": code, "instalacao_publica_id": str(uuid4())}
    )
    token = response.json()["token"]
    async with system.admin.connect() as conn:
        serialized = json.dumps(
            (
                await conn.scalars(
                    text("SELECT row_to_json(i) FROM coleta_instalacoes i WHERE usuario_id=:u"),
                    {"u": system.user},
                )
            ).all()
        )
        serialized += json.dumps(
            (
                await conn.scalars(
                    text("SELECT row_to_json(c) FROM coleta_pairing_codes c WHERE usuario_id=:u"),
                    {"u": system.user},
                )
            ).all()
        )
    assert token not in serialized and code not in serialized
    assert coleta_tokens.digest(token) in serialized
    assert coleta_tokens.digest(code, "pairing") in serialized
    assert not {"fingerprint", "bookmaker", "casa", "user_agent", "ip"}.intersection(
        ColetaInstalacao.__table__.columns.keys()
    )


@pytest.mark.parametrize("kind", ["expired", "inactive", "legacy"])
async def test_expired_inactive_or_legacy_credentials_cannot_collect(system, kind):
    paired = await system.pair()
    async with system.admin.begin() as conn:
        if kind == "expired":
            await conn.execute(
                update(ColetaInstalacao)
                .where(ColetaInstalacao.id == paired["instalacao_id"])
                .values(expira_em=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif kind == "inactive":
            await conn.execute(
                update(models.Usuario).where(models.Usuario.id == system.user).values(ativo=False)
            )
        else:
            paired["token"] = "synthetic-legacy-only"
            await conn.execute(
                insert(models.ColetaToken).values(
                    usuario_id=system.user, token_hash=coleta_tokens.digest(paired["token"])
                )
            )
    response = await system.http.post(PREFIX, headers=token_headers(paired), json=batch())
    assert response.status_code == 403 and system.queued == []


async def test_global_quota_is_shared_by_distinct_clients(system, monkeypatch):
    settings = get_settings().model_copy(update={"COLETA_PAIRING_GLOBAL_LIMIT": 1})
    monkeypatch.setattr(coleta_tokens, "get_settings", lambda: settings)
    async with AsyncSession(system.engine) as session:
        await coleta_tokens.admit(session, action="exchange", identity="first")
    async with AsyncSession(system.engine) as session:
        with pytest.raises(Exception) as error:
            await coleta_tokens.admit(session, action="exchange", identity="second")
        assert error.value.status_code == 429


async def test_lost_exchange_transaction_preserves_code_for_retry(system, monkeypatch):
    from sqlalchemy.exc import OperationalError

    code = await system.code()
    original = AsyncSession.commit
    commits = 0

    async def failed_commit(session):
        nonlocal commits
        commits += 1
        if commits == 2:  # Admission commits first, then the pairing transaction.
            raise OperationalError("synthetic commit", {}, Exception("cti_SENTINEL"))
        await original(session)

    body = {"codigo": code, "instalacao_publica_id": str(uuid4())}
    monkeypatch.setattr(AsyncSession, "commit", failed_commit)
    response = await system.http.post(PREFIX + "/pairing-exchange", json=body)
    assert response.status_code in (500, 503) and "SENTINEL" not in response.text
    monkeypatch.setattr(AsyncSession, "commit", original)
    async with system.admin.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count())
                .select_from(ColetaInstalacao)
                .where(ColetaInstalacao.usuario_id == system.user)
            )
            == 0
        )
    assert (await system.http.post(PREFIX + "/pairing-exchange", json=body)).status_code == 200


async def test_concurrent_reconnect_keeps_one_installation_and_one_current_token(system):
    codes = [await system.code(), await system.code()]
    public = str(uuid4())
    responses = await asyncio.gather(*[
        system.http.post(
            PREFIX + "/pairing-exchange", json={"codigo": code, "instalacao_publica_id": public}
        )
        for code in codes
    ])
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["instalacao_id"] for response in responses}) == 1
    statuses = [
        await system.http.get(PREFIX + "/status", headers=token_headers(response.json()))
        for response in responses
    ]
    assert sorted(response.status_code for response in statuses) == [200, 403]


async def test_legacy_upgrade_requires_repair_and_roundtrip_never_reactivates(banco):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.pool import NullPool

    name = "pairing_migration_" + uuid4().hex
    admin_url = make_url(banco.url_admin)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    target = admin_url.set(database=name).render_as_string(hide_password=False)
    config = Config(str(Path("alembic.ini").resolve()))
    previous = os.environ["DATABASE_URL"]
    engine = create_async_engine(target, poolclass=NullPool)
    try:
        os.environ["DATABASE_URL"] = target
        await asyncio.to_thread(command.upgrade, config, "a9d6e3f1c210")
        async with engine.begin() as conn:
            uid = await conn.scalar(
                text(
                    "INSERT INTO usuarios(email,nome) VALUES ('legacy@synthetic.test','Legacy') RETURNING id"
                )
            )
            await conn.execute(
                text("INSERT INTO coleta_token(usuario_id,token_hash) VALUES (:u,:hash)"),
                {"u": uid, "hash": "f" * 64},
            )
        # Pairing's published migration is exercised independently of billing rollback.
        await asyncio.to_thread(command.upgrade, config, "c107pair2026")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM coleta_instalacoes WHERE revogado_em IS NOT NULL AND token_hash IS NULL"
                    )
                )
                == 1
            )
            assert await conn.scalar(text("SELECT ativo FROM coleta_token")) is False
        await asyncio.to_thread(command.downgrade, config, "a9d6e3f1c210")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT ativo FROM coleta_token")) is False
        await asyncio.to_thread(command.upgrade, config, "c107pair2026")
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE coleta_instalacoes SET pareado_em=now() WHERE usuario_id=:u"),
                {"u": uid},
            )
        with pytest.raises(DBAPIError, match="preserve installation inventory"):
            await asyncio.to_thread(command.downgrade, config, "a9d6e3f1c210")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version")) == "c107pair2026"
            )
            assert await conn.scalar(text("SELECT ativo FROM coleta_token")) is False
    finally:
        os.environ["DATABASE_URL"] = previous
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()
