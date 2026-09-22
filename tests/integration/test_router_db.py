from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import Depends, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwk, jwt
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia import main, models
from bancaemdia.auth import jwt as auth_jwt
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.config import get_settings
from bancaemdia.db import session as db_session
from bancaemdia.db.session import get_db
from bancaemdia.middleware import router
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = pytest.mark.xdist_group("postgres")

PAPEL = SENHA = "bancaemdia_app"


@pytest.fixture(scope="module")
def chave():
    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privada = par.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    publica = par.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return privada, {**jwk.construct(publica, "RS256").to_dict(), "kid": "k1"}


@pytest.fixture
def replica():
    url = os.environ.get("TEST_REPLICA_DATABASE_URL")
    if not url:
        pytest.skip("standby tests need TEST_REPLICA_DATABASE_URL pointing at a hot standby")
    app = make_url(url).set(username=PAPEL, password=SENHA).render_as_string(hide_password=False)
    return SimpleNamespace(url_admin=url, url_app=app)


def _relogio():
    class Relogio:
        def __init__(self):
            self.agora = 1000.0

        def __call__(self):
            return self.agora

    return Relogio()


def _aposta(usuario_id):
    return {
        "usuario_id": usuario_id,
        "chave": f"m:{uuid4().hex[:8]}",
        "origem": "manual",
        "stake_unidades": 1.0,
        "stake_centavos": 10_000,
    }


def _cabecalho(privada, usuario_id):
    settings = get_settings()
    corpo = {
        "sub": str(usuario_id),
        "aud": settings.JWT_AUDIENCE,
        "iss": settings.JWT_ISSUER,
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(corpo, privada, algorithm="RS256", headers={"kid": "k1"})
    return {"Authorization": f"Bearer {token}"}


class Rotas:
    async def estado(self, session: AsyncSession) -> dict[str, object]:
        leitura, usuario, recuperacao = (
            await session.execute(
                text(
                    "SELECT current_setting('default_transaction_read_only'),"
                    " current_setting('app.current_user_id', true), pg_is_in_recovery()"
                )
            )
        ).one()
        chaves = sorted((await session.execute(select(models.Aposta.chave))).scalars())
        return {
            "somente_leitura": leitura,
            "usuario": usuario,
            "recuperacao": recuperacao,
            "chaves": chaves,
        }

    async def ler(self, session: AsyncSession = Depends(get_db)) -> dict[str, object]:
        return await self.estado(session)

    async def gravar(
        self, request: Request, session: AsyncSession = Depends(get_db)
    ) -> dict[str, object]:
        aposta = await ApostaRepo().upsert_idempotent(session, _aposta(request.state.usuario_id))
        estado = await self.estado(session)
        await session.commit()
        return {**estado, "criada": aposta.chave}


def _sessoes(url, replica):
    engine = create_async_engine(
        url, poolclass=NullPool, connect_args=db_session.connect_args(replica=replica)
    )
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _cliente(monkeypatch, chave, url_primario, url_replica):
    _, publica = chave

    def responder(request):
        return httpx.Response(200, json={"keys": [publica]})

    cache = auth_jwt.JWKSCache("https://issuer.test/jwks", "RS256", httpx.MockTransport(responder))
    relogio = _relogio()
    escritas = router.RecentWrites(clock=relogio)
    rotas = Rotas()
    monkeypatch.setattr(db_session, "SessionLocal", _sessoes(url_primario, replica=False))
    monkeypatch.setattr(db_session, "ReplicaSession", _sessoes(url_replica, replica=True))
    monkeypatch.setattr(db_session, "replica_lag", db_session.ReplicaLagCheck())
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)
    monkeypatch.setattr(router, "get_recent_writes", lambda: escritas)
    monkeypatch.setattr(
        main.app.router,
        "routes",
        [
            *main.app.router.routes,
            APIRoute("/api/v1/teste/apostas", rotas.ler, methods=["GET"]),
            APIRoute("/api/v1/teste/apostas", rotas.gravar, methods=["POST"]),
            APIRoute("/api/v1/teste/escreve", rotas.gravar, methods=["GET"]),
        ],
    )
    return TestClient(main.app), relogio


async def _usuario_com_aposta(url: str) -> tuple[int, str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            usuario = await UsuarioRepo().create(session, f"{uuid4().hex[:10]}@teste.local", "T")
            await session.commit()
        async with AsyncSession(engine) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": str(usuario.id)},
            )
            aposta = await ApostaRepo().upsert_idempotent(session, _aposta(usuario.id))
            await session.commit()
        assert aposta.chave is not None
        return usuario.id, aposta.chave
    finally:
        await engine.dispose()


async def _uma_linha(url: str, sql: str, **parametros: object) -> object:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text(sql), parametros)
    finally:
        await engine.dispose()


def _esperar_a_replica(url_primario: str, url_replica: str) -> None:
    lsn = asyncio.run(_uma_linha(url_primario, "SELECT pg_current_wal_lsn()::text"))
    for _ in range(100):
        if asyncio.run(
            _uma_linha(
                url_replica,
                "SELECT pg_last_wal_replay_lsn() >= CAST(CAST(:lsn AS text) AS pg_lsn)",
                lsn=lsn,
            )
        ):
            return
        time.sleep(0.1)
    raise AssertionError(f"the standby did not replay {lsn} in time")


def test_writes_use_the_primary_and_reads_the_read_only_pool_each_user_seeing_their_rows(
    banco, chave, monkeypatch
) -> None:
    privada, _ = chave
    ana, de_ana = asyncio.run(_usuario_com_aposta(banco.url_app))
    bia, de_bia = asyncio.run(_usuario_com_aposta(banco.url_app))
    cliente, relogio = _cliente(monkeypatch, chave, banco.url_app, banco.url_app)

    gravou = cliente.post("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()
    relogio.agora += router.WRITE_WINDOW_SECONDS
    da_ana = cliente.get("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()
    da_bia = cliente.get("/api/v1/teste/apostas", headers=_cabecalho(privada, bia)).json()

    assert (gravou["somente_leitura"], gravou["usuario"]) == ("off", str(ana))
    assert (da_ana["somente_leitura"], da_ana["usuario"]) == ("on", str(ana))
    assert da_ana["chaves"] == sorted([de_ana, gravou["criada"]])
    assert (da_bia["somente_leitura"], da_bia["chaves"]) == ("on", [de_bia])


def test_a_bet_just_created_is_read_back_from_the_primary(banco, chave, monkeypatch) -> None:
    privada, _ = chave
    ana, _ = asyncio.run(_usuario_com_aposta(banco.url_app))
    cliente, _ = _cliente(monkeypatch, chave, banco.url_app, banco.url_app)

    gravou = cliente.post("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()
    leu = cliente.get("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()

    assert leu["somente_leitura"] == "off"
    assert gravou["criada"] in leu["chaves"]


def test_a_read_that_writes_is_refused_by_the_database(banco, chave, monkeypatch) -> None:
    privada, _ = chave
    ana, _ = asyncio.run(_usuario_com_aposta(banco.url_app))
    cliente, _ = _cliente(monkeypatch, chave, banco.url_app, banco.url_app)

    with pytest.raises(DBAPIError) as erro:
        cliente.get("/api/v1/teste/escreve", headers=_cabecalho(privada, ana))

    assert erro.value.orig.sqlstate == "25006"


def test_lag_is_unknown_on_a_server_that_is_not_a_standby(banco) -> None:
    async def atraso() -> float | None:
        engine = create_async_engine(banco.url_app, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await db_session.replica_lag_seconds(conn)
        finally:
            await engine.dispose()

    assert asyncio.run(atraso()) is None


def test_reads_land_on_the_standby_under_rls_and_writes_on_the_primary(
    banco, replica, chave, monkeypatch
) -> None:
    privada, _ = chave
    ana, de_ana = asyncio.run(_usuario_com_aposta(banco.url_app))
    asyncio.run(_usuario_com_aposta(banco.url_app))
    cliente, relogio = _cliente(monkeypatch, chave, banco.url_app, replica.url_app)

    gravou = cliente.post("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()
    _esperar_a_replica(banco.url_admin, replica.url_admin)
    relogio.agora += router.WRITE_WINDOW_SECONDS
    leu = cliente.get("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()

    assert (gravou["recuperacao"], gravou["usuario"]) == (False, str(ana))
    assert (leu["recuperacao"], leu["usuario"]) == (True, str(ana))
    assert leu["chaves"] == sorted([de_ana, gravou["criada"]])


def test_a_new_bet_is_seen_for_five_seconds_while_the_standby_is_behind(
    banco, replica, chave, monkeypatch
) -> None:
    privada, _ = chave
    ana, _ = asyncio.run(_usuario_com_aposta(banco.url_app))
    _esperar_a_replica(banco.url_admin, replica.url_admin)
    cliente, relogio = _cliente(monkeypatch, chave, banco.url_app, replica.url_app)

    asyncio.run(_uma_linha(replica.url_admin, "SELECT pg_wal_replay_pause()::text"))
    try:
        gravou = cliente.post("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()
        logo = cliente.get("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()
        relogio.agora += router.WRITE_WINDOW_SECONDS
        depois = cliente.get("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()
    finally:
        asyncio.run(_uma_linha(replica.url_admin, "SELECT pg_wal_replay_resume()::text"))
    _esperar_a_replica(banco.url_admin, replica.url_admin)
    em_dia = cliente.get("/api/v1/teste/apostas", headers=_cabecalho(privada, ana)).json()

    assert (logo["recuperacao"], gravou["criada"] in logo["chaves"]) == (False, True)
    assert (depois["recuperacao"], gravou["criada"] in depois["chaves"]) == (True, False)
    assert (em_dia["recuperacao"], gravou["criada"] in em_dia["chaves"]) == (True, True)


def test_standby_lag_is_zero_when_caught_up_and_grows_while_replay_is_paused(
    banco, replica
) -> None:
    async def atraso() -> float | None:
        engine = create_async_engine(replica.url_app, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await db_session.replica_lag_seconds(conn)
        finally:
            await engine.dispose()

    em_dia = None
    for _ in range(100):
        em_dia = asyncio.run(atraso())
        if em_dia == 0:
            break
        time.sleep(0.1)
    asyncio.run(_uma_linha(replica.url_admin, "SELECT pg_wal_replay_pause()::text"))
    try:
        asyncio.run(_usuario_com_aposta(banco.url_app))
        for _ in range(100):
            if asyncio.run(
                _uma_linha(
                    replica.url_admin,
                    "SELECT pg_last_wal_receive_lsn() > pg_last_wal_replay_lsn()",
                )
            ):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the standby did not receive WAL while replay was paused")
        atrasada = asyncio.run(atraso())
    finally:
        asyncio.run(_uma_linha(replica.url_admin, "SELECT pg_wal_replay_resume()::text"))

    assert em_dia == pytest.approx(0.0)
    assert atrasada is not None and atrasada > 0
