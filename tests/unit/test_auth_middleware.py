from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from bancaemdia import main
from bancaemdia.api import deps
from bancaemdia.api.v1 import coleta
from bancaemdia.auth import jwt as auth_jwt
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.middleware import JWTAuthMiddleware
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db
from bancaemdia.domain.registros import Usuario
from bancaemdia.middleware.rls import RLSMiddleware


@pytest.fixture(scope="module")
def chave():
    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privada = par.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    return privada, {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(par.public_key())),
        "kid": "k1",
    }


def _token(privada, **claims):
    settings = get_settings()
    corpo = {
        "sub": "7",
        "aud": settings.JWT_AUDIENCE,
        "iss": settings.JWT_ISSUER,
        "exp": int(time.time()) + 60,
        **claims,
    }
    return jwt.encode(corpo, privada, algorithm="RS256", headers={"kid": "k1"})


class Rotas:
    async def quem_sou(self, request: Request) -> dict[str, object]:
        return {"usuario_id": request.state.usuario_id, "contexto": current_user_id.get()}

    async def recurso(self, request: Request, nome: str) -> dict[str, object]:
        return {"nome": nome, "usuario_id": getattr(request.state, "usuario_id", None)}

    async def eu(self, usuario: Usuario = Depends(deps.get_current_user)) -> dict[str, object]:
        return {"id": usuario.id, "nome": usuario.nome}


def _cliente(monkeypatch, cache, usuarios=()):
    class UsuarioRepo:
        async def get_by_id(self, session, id_):
            return next((u for u in usuarios if u.id == id_), None)

    class Sessoes:
        async def abrir(self):
            yield None

    rotas = Rotas()
    monkeypatch.setattr(
        main.app.router,
        "routes",
        [
            *main.app.router.routes,
            APIRoute("/api/v1/quem-sou", rotas.quem_sou, dependency_overrides_provider=main.app),
            APIRoute("/api/v1/eu", rotas.eu, dependency_overrides_provider=main.app),
            APIRoute("/api/v1/{nome}", rotas.recurso),
        ],
    )
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)
    monkeypatch.setattr(deps, "UsuarioRepo", UsuarioRepo)
    monkeypatch.setitem(main.app.dependency_overrides, get_db, Sessoes().abrir)
    return TestClient(main.app)


def _chaves(jwk_publica):
    def responder(request):
        return httpx.Response(200, json={"keys": [jwk_publica]})

    return auth_jwt.JWKSCache("https://issuer.test/jwks", "RS256", httpx.MockTransport(responder))


def _usuario(id_, ativo=True):
    return Usuario(
        id=id_, email=f"{id_}@teste.local", nome=f"U{id_}", criado_em=datetime.now(UTC), ativo=ativo
    )


def test_valid_token_reaches_the_route_with_the_user_set_for_rls(monkeypatch, chave) -> None:
    privada, publica = chave
    cliente = _cliente(monkeypatch, _chaves(publica))

    resposta = cliente.get(
        "/api/v1/quem-sou", headers={"Authorization": f"Bearer {_token(privada)}"}
    )

    assert resposta.status_code == 200
    assert resposta.json() == {"usuario_id": 7, "contexto": "7"}


def test_missing_token_is_401_with_a_bare_bearer_challenge(monkeypatch, chave) -> None:
    _, publica = chave
    cliente = _cliente(monkeypatch, _chaves(publica))

    sem_cabecalho = cliente.get("/api/v1/quem-sou")
    outro_esquema = cliente.get("/api/v1/quem-sou", headers={"Authorization": "Basic dXNlcjpwdw=="})
    vazio = cliente.get("/api/v1/quem-sou", headers={"Authorization": "Bearer "})

    for resposta in (sem_cabecalho, outro_esquema, vazio):
        assert resposta.status_code == 401
        assert resposta.headers["WWW-Authenticate"] == "Bearer"
        assert resposta.json() == {"detail": "Not authenticated"}


@pytest.mark.parametrize(
    "claims", [{"exp": 1}, {"aud": "other-api"}, {"iss": "other-issuer"}, {"sub": "abc"}]
)
def test_expired_or_foreign_token_is_401_invalid_token(monkeypatch, chave, claims) -> None:
    privada, publica = chave
    cliente = _cliente(monkeypatch, _chaves(publica))

    resposta = cliente.get(
        "/api/v1/quem-sou", headers={"Authorization": f"bearer {_token(privada, **claims)}"}
    )

    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert resposta.json() == {"detail": "Invalid token"}


def test_without_signing_keys_authentication_answers_503(monkeypatch, chave) -> None:
    privada, _ = chave
    cliente = _cliente(monkeypatch, auth_jwt.JWKSCache(None, "RS256"))

    resposta = cliente.get(
        "/api/v1/quem-sou", headers={"Authorization": f"Bearer {_token(privada)}"}
    )

    assert resposta.status_code == 503
    assert "WWW-Authenticate" not in resposta.headers


def test_unknown_paths_need_a_token_too(monkeypatch, chave) -> None:
    _, publica = chave
    cliente = _cliente(monkeypatch, _chaves(publica))

    assert cliente.get("/nao-existe").status_code == 401


@pytest.mark.parametrize("caminho", ["/api/v1/coleta%3Fx", "/api/v1/coleta%23x", "/health%3F"])
def test_an_encoded_query_or_fragment_does_not_make_a_route_public(
    monkeypatch, chave, caminho
) -> None:
    _, publica = chave
    cliente = _cliente(monkeypatch, _chaves(publica))

    resposta = cliente.get(caminho)

    assert resposta.status_code == 401


def test_public_paths_answer_without_a_bearer_token(monkeypatch, chave) -> None:
    _, publica = chave

    class Report:
        status_code = 200

        def as_dict(self):
            return {"status": "ready", "checks": {}}

    class Readiness:
        async def check(self):
            return Report()

    class Filas:
        def collect(self):
            return iter(())

    monkeypatch.setattr(main, "get_readiness_checker", Readiness)
    monkeypatch.setattr(main, "breaker_states", lambda: {"anthropic": "closed"})
    monkeypatch.setattr(main, "get_queue_depth_collector", Filas)
    monkeypatch.setattr(coleta.limiter, "enabled", False)
    cliente = _cliente(monkeypatch, _chaves(publica))

    assert cliente.get("/health").status_code == 200
    assert cliente.get("/ready").status_code == 200
    assert cliente.get("/metrics").status_code == 200
    assert cliente.get("/docs").status_code == 200
    for caminho in ("/coleta", "/api/v1/coleta"):
        resposta = cliente.post(caminho, json={}, headers={"Authorization": "Bearer lixo"})
        assert resposta.status_code == 403


def test_public_paths_are_matched_like_the_router_behind_a_root_path() -> None:
    def pedido(path, root_path=""):
        return SimpleNamespace(scope={"path": path, "root_path": root_path})

    assert auth_middleware.route_path(pedido("/prefixo/health", "/prefixo")) == "/health"
    assert auth_middleware.route_path(pedido("/prefixohealth", "/prefixo")) == "/prefixohealth"
    assert auth_middleware.route_path(pedido("/health")) == "/health"


def test_authentication_is_registered_outside_the_rls_middleware() -> None:
    camadas = [camada.cls for camada in main.app.user_middleware]

    assert camadas.index(JWTAuthMiddleware) < camadas.index(RLSMiddleware)


def test_current_user_is_loaded_and_must_be_active(monkeypatch, chave) -> None:
    privada, publica = chave
    cliente = _cliente(monkeypatch, _chaves(publica), [_usuario(7), _usuario(8, ativo=False)])

    ativo = cliente.get("/api/v1/eu", headers={"Authorization": f"Bearer {_token(privada)}"})
    inativo = cliente.get(
        "/api/v1/eu", headers={"Authorization": f"Bearer {_token(privada, sub='8')}"}
    )
    sumido = cliente.get(
        "/api/v1/eu", headers={"Authorization": f"Bearer {_token(privada, sub='9')}"}
    )

    assert (ativo.status_code, ativo.json()) == (200, {"id": 7, "nome": "U7"})
    for resposta in (inativo, sumido):
        assert resposta.status_code == 401
        assert resposta.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_current_user_dependency_documents_the_bearer_scheme() -> None:
    app = FastAPI()
    app.add_api_route("/eu", Rotas().eu)

    esquema = app.openapi()

    assert esquema["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer"}
    }
    assert esquema["paths"]["/eu"]["get"]["security"] == [{"HTTPBearer": []}]
