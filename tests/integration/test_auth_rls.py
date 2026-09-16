from __future__ import annotations

import asyncio
import time
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
from fastapi import Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwk, jwt
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia import main, models
from bancaemdia.api import deps
from bancaemdia.auth import jwt as auth_jwt
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.config import get_settings
from bancaemdia.db import session as db_session
from bancaemdia.db.session import get_db
from bancaemdia.domain.registros import Usuario
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

pytestmark = pytest.mark.xdist_group("postgres")


@pytest.fixture(scope="module")
def chave():
    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privada = par.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    publica = par.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return privada, {**jwk.construct(publica, "RS256").to_dict(), "kid": "k1"}


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


def _aposta(usuario_id):
    return {
        "usuario_id": usuario_id,
        "chave": f"m:{uuid4().hex[:8]}",
        "origem": "manual",
        "stake_unidades": 1.0,
        "stake_centavos": 10_000,
    }


class Rotas:
    async def minhas_chaves(self, session: AsyncSession = Depends(get_db)) -> list[str]:
        return sorted((await session.execute(select(models.Aposta.chave))).scalars())

    async def gravar_e_ler(
        self,
        usuario: Usuario = Depends(deps.get_current_user),
        session: AsyncSession = Depends(get_db),
    ) -> list[str]:
        await ApostaRepo().upsert_idempotent(session, _aposta(usuario.id))
        await session.commit()
        return sorted((await session.execute(select(models.Aposta.chave))).scalars())


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


async def _desativar(url: str, usuario_id: int) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": str(usuario_id)},
            )
            await session.execute(
                update(models.Usuario).where(models.Usuario.id == usuario_id).values(ativo=False)
            )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def cliente(banco, chave, monkeypatch):
    _, publica = chave

    def responder(request):
        return httpx.Response(200, json={"keys": [publica]})

    cache = auth_jwt.JWKSCache("https://issuer.test/jwks", "RS256", httpx.MockTransport(responder))
    sessoes = async_sessionmaker(
        create_async_engine(banco.url_app, poolclass=NullPool),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    monkeypatch.setattr(db_session, "SessionLocal", sessoes)
    monkeypatch.setattr(db_session, "ReplicaSession", sessoes)
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)
    rotas = Rotas()
    monkeypatch.setattr(
        main.app.router,
        "routes",
        [
            *main.app.router.routes,
            APIRoute("/api/v1/minhas-chaves", rotas.minhas_chaves),
            APIRoute("/api/v1/gravar-e-ler", rotas.gravar_e_ler, methods=["POST"]),
        ],
    )
    return TestClient(main.app)


def test_each_token_sees_only_its_users_bets_through_the_rls_role(banco, chave, cliente) -> None:
    privada, _ = chave
    ana, de_ana = asyncio.run(_usuario_com_aposta(banco.url_app))
    bia, de_bia = asyncio.run(_usuario_com_aposta(banco.url_app))

    da_ana = cliente.get("/api/v1/minhas-chaves", headers=_cabecalho(privada, ana))
    da_bia = cliente.get("/api/v1/minhas-chaves", headers=_cabecalho(privada, bia))
    sem_token = cliente.get("/api/v1/minhas-chaves")

    assert (da_ana.status_code, da_ana.json()) == (200, [de_ana])
    assert (da_bia.status_code, da_bia.json()) == (200, [de_bia])
    assert sem_token.status_code == 401


def test_a_request_still_sees_its_rows_after_committing(banco, chave, cliente) -> None:
    privada, _ = chave
    ana, de_ana = asyncio.run(_usuario_com_aposta(banco.url_app))

    resposta = cliente.post("/api/v1/gravar-e-ler", headers=_cabecalho(privada, ana))

    assert resposta.status_code == 200
    chaves = resposta.json()
    assert len(chaves) == 2
    assert de_ana in chaves


def test_a_deactivated_user_is_refused_even_with_a_valid_token(banco, chave, cliente) -> None:
    privada, _ = chave
    ana, _ = asyncio.run(_usuario_com_aposta(banco.url_app))
    asyncio.run(_desativar(banco.url_app, ana))

    resposta = cliente.post("/api/v1/gravar-e-ler", headers=_cabecalho(privada, ana))

    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
