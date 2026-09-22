from __future__ import annotations

import time
from decimal import Decimal

import asyncpg
import httpx
import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwk, jwt
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.requests import Request as StarletteRequest

from bancaemdia import main
from bancaemdia.api.v1 import coleta
from bancaemdia.auth import jwt as auth_jwt
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.middleware import JWTAuthMiddleware
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id, use_primary
from bancaemdia.db import session as db_session
from bancaemdia.db.session import get_db
from bancaemdia.middleware import router
from bancaemdia.middleware.rls import RLSMiddleware
from bancaemdia.middleware.router import RouterMiddleware


def _relogio():
    class Relogio:
        def __init__(self):
            self.agora = 1000.0

        def __call__(self):
            return self.agora

    return Relogio()


def _pedido(metodo="GET", caminho="/api/v1/apostas", consulta=b"", cabecalhos=()):
    return StarletteRequest({
        "type": "http",
        "method": metodo,
        "path": caminho,
        "root_path": "",
        "query_string": consulta,
        "headers": [(nome.lower().encode(), valor.encode()) for nome, valor in cabecalhos],
    })


@pytest.mark.parametrize(
    ("metodo", "esperado"),
    [
        ("POST", True),
        ("PUT", True),
        ("PATCH", True),
        ("DELETE", True),
        ("GET", False),
        ("HEAD", False),
        ("OPTIONS", False),
    ],
)
def test_writes_go_to_the_primary_and_safe_reads_to_the_replica(metodo, esperado) -> None:
    assert router.needs_primary(_pedido(metodo), 7, router.RecentWrites()) is esperado


@pytest.mark.parametrize(
    ("cabecalhos", "esperado"),
    [
        ((("X-Read-Replica", "false"),), True),
        ((("x-read-replica", " FALSE "),), True),
        ((("X-Read-Replica", "true"),), False),
        ((), False),
    ],
)
def test_the_read_replica_header_can_force_the_primary(cabecalhos, esperado) -> None:
    pedido = _pedido(cabecalhos=cabecalhos)

    assert router.needs_primary(pedido, 7, router.RecentWrites()) is esperado


@pytest.mark.parametrize(
    ("caminho", "consulta", "esperado"),
    [
        ("/api/v1/painel", b"fresh=true", True),
        ("/api/v1/painel", b"de=2026-09&fresh=True", True),
        ("/api/v1/painel", b"fresh=1", True),
        ("/api/v1/painel", b"fresh=false", False),
        ("/api/v1/painel", b"fresh=0", False),
        ("/api/v1/painel", b"", False),
        ("/api/v1/painel/export", b"fresh=true", False),
        ("/api/v1/apostas", b"fresh=true", False),
    ],
)
def test_only_a_fresh_painel_is_read_from_the_primary(caminho, consulta, esperado) -> None:
    pedido = _pedido(caminho=caminho, consulta=consulta)

    assert router.needs_primary(pedido, None, router.RecentWrites()) is esperado


def test_fresh_means_what_a_fastapi_bool_query_parameter_means() -> None:
    class Painel:
        def ver(self, fresh: bool = False) -> dict[str, bool]:
            return {"fresh": fresh}

    app = FastAPI()
    app.add_api_route("/api/v1/painel", Painel().ver)
    cliente = TestClient(app)
    valores = ["true", "True", "1", "yes", "on", "t", "y", "false", "0", "no", "off", "f", "n"]

    lido = {
        valor: cliente.get(f"/api/v1/painel?fresh={valor}").json()["fresh"] for valor in valores
    }
    roteado = {
        valor: router.needs_primary(
            _pedido(caminho="/api/v1/painel", consulta=f"fresh={valor}".encode()),
            None,
            router.RecentWrites(),
        )
        for valor in valores
    }

    assert roteado == lido
    assert sum(lido.values()) == 7


def test_recent_writes_last_five_seconds_per_user() -> None:
    relogio = _relogio()
    escritas = router.RecentWrites(clock=relogio)

    escritas.mark(7)
    relogio.agora += 4.9
    dentro = (escritas.recent(7), escritas.recent(8))
    relogio.agora += 0.1
    fora = escritas.recent(7)

    assert dentro == (True, False)
    assert fora is False
    assert escritas.expires == {}


def test_marking_a_write_drops_the_expired_marks() -> None:
    relogio = _relogio()
    escritas = router.RecentWrites(clock=relogio)
    escritas.mark(7)
    relogio.agora += router.WRITE_WINDOW_SECONDS

    escritas.mark(8)

    assert escritas.expires == {8: relogio.agora + router.WRITE_WINDOW_SECONDS}


def _sessoes(monkeypatch):
    abertas = []

    class Sessao:
        bind = None

        def __init__(self, nome):
            self.nome = nome
            self.executados = []

        async def __aenter__(self):
            abertas.append(self.nome)
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement, params=None):
            self.executados.append((statement.text, params))

    monkeypatch.setattr(db_session, "SessionLocal", lambda: Sessao("primario"))
    monkeypatch.setattr(db_session, "ReplicaSession", lambda: Sessao("replica"))
    return abertas


async def _uma(gerador):
    return [sessao async for sessao in gerador]


async def test_get_db_follows_the_router_and_the_explicit_ones_do_not(monkeypatch) -> None:
    abertas = _sessoes(monkeypatch)

    await _uma(db_session.get_db())
    token = use_primary.set(False)
    try:
        await _uma(db_session.get_db())
        await _uma(db_session.get_db_primary())
    finally:
        use_primary.reset(token)
    await _uma(db_session.get_db_replica())

    assert abertas == ["primario", "replica", "primario", "replica"]


def _conexao_com_atraso(monkeypatch, *atrasos):
    class Conexao:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Replica:
        def __init__(self):
            self.consultas = []
            self.fila = list(atrasos)

        async def atraso(self, conexao):
            self.consultas.append(conexao)
            return self.fila.pop(0)

    replica = Replica()
    monkeypatch.setattr(AsyncEngine, "connect", lambda self: Conexao())
    monkeypatch.setattr(db_session, "replica_lag_seconds", replica.atraso)
    return replica.consultas


def _sessao_da_replica():
    class Sessao:
        bind = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/x")

    return Sessao()


async def test_lag_seconds_come_back_as_a_float_or_none() -> None:
    class Conexao:
        def __init__(self, valor):
            self.valor = valor

        async def scalar(self, statement):
            return self.valor

    assert await db_session.replica_lag_seconds(Conexao(Decimal("12.5"))) == pytest.approx(12.5)
    assert await db_session.replica_lag_seconds(Conexao(None)) is None


async def test_replica_lag_is_logged_only_above_thirty_seconds(monkeypatch) -> None:
    consultas = _conexao_com_atraso(monkeypatch, None, 0.0, 30.0, 45.25)
    relogio = _relogio()
    checagem = db_session.ReplicaLagCheck(clock=relogio)
    sessao = _sessao_da_replica()

    with structlog.testing.capture_logs() as avisos:
        for _ in range(4):
            await checagem.run(sessao)
            relogio.agora += db_session.LAG_CHECK_SECONDS

    assert len(consultas) == 4
    assert avisos == [{"event": "replica_lag", "seconds": 45.2, "log_level": "warning"}]


async def test_replica_lag_is_checked_at_most_once_per_interval(monkeypatch) -> None:
    consultas = _conexao_com_atraso(monkeypatch, 0.0, 0.0)
    relogio = _relogio()
    checagem = db_session.ReplicaLagCheck(clock=relogio)
    sessao = _sessao_da_replica()

    await checagem.run(sessao)
    relogio.agora += 29
    await checagem.run(sessao)
    relogio.agora += 1
    await checagem.run(sessao)

    assert len(consultas) == 2


async def test_an_unreachable_replica_only_logs_that_the_lag_is_unknown(monkeypatch) -> None:
    class Quebrada:
        async def __aenter__(self):
            raise OperationalError("SELECT 1", {}, Exception("down"))

        async def __aexit__(self, *args):
            return False

    class Lotada:
        async def conectar(self, *args, **kwargs):
            raise asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already")

    checagem = db_session.ReplicaLagCheck(clock=_relogio())
    fora_do_ar = db_session.ReplicaLagCheck(clock=_relogio())
    lotada = db_session.ReplicaLagCheck(clock=_relogio())
    sessao = _sessao_da_replica()

    class SessaoLotada:
        bind = create_async_engine("postgresql+asyncpg://", async_creator=Lotada().conectar)

    with structlog.testing.capture_logs() as avisos:
        await fora_do_ar.run(sessao)
        await lotada.run(SessaoLotada())
        monkeypatch.setattr(AsyncEngine, "connect", lambda self: Quebrada())
        await checagem.run(sessao)

    assert [(a["event"], a["error"]) for a in avisos] == [
        ("replica_lag_unavailable", "ConnectionRefusedError"),
        ("replica_lag_unavailable", "TooManyConnectionsError"),
        ("replica_lag_unavailable", "OperationalError"),
    ]


async def test_sessions_without_an_engine_skip_the_lag_check() -> None:
    class Sessao:
        bind = None

    checagem = db_session.ReplicaLagCheck(clock=_relogio())

    await checagem.run(Sessao())

    assert checagem.next_at is None


@pytest.fixture(scope="module")
def chave():
    par = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    privada = par.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    publica = par.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return privada, {**jwk.construct(publica, "RS256").to_dict(), "kid": "k1"}


def _cabecalho(privada, usuario_id, **extra):
    settings = get_settings()
    corpo = {
        "sub": str(usuario_id),
        "aud": settings.JWT_AUDIENCE,
        "iss": settings.JWT_ISSUER,
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(corpo, privada, algorithm="RS256", headers={"kid": "k1"})
    return {"Authorization": f"Bearer {token}", **extra}


class Rotas:
    async def banco(self, request: Request, session=Depends(get_db)) -> dict[str, object]:
        return {"banco": session.nome}


def _cliente(monkeypatch, chave, relogio):
    _, publica = chave

    def responder(request):
        return httpx.Response(200, json={"keys": [publica]})

    cache = auth_jwt.JWKSCache("https://issuer.test/jwks", "RS256", httpx.MockTransport(responder))
    escritas = router.RecentWrites(clock=relogio)
    _sessoes(monkeypatch)
    rotas = Rotas()
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)
    monkeypatch.setattr(router, "get_recent_writes", lambda: escritas)
    monkeypatch.setattr(coleta.limiter, "enabled", False)
    monkeypatch.setattr(
        main.app.router,
        "routes",
        [
            *main.app.router.routes,
            APIRoute("/api/v1/teste", rotas.banco, methods=["GET", "POST"]),
            APIRoute("/api/v1/painel", rotas.banco),
        ],
    )
    return TestClient(main.app), escritas


def test_a_user_reads_the_primary_for_five_seconds_after_writing(monkeypatch, chave) -> None:
    privada, _ = chave
    relogio = _relogio()
    cliente, escritas = _cliente(monkeypatch, chave, relogio)

    def banco(metodo, usuario, caminho="/api/v1/teste", **extra):
        resposta = cliente.request(metodo, caminho, headers=_cabecalho(privada, usuario, **extra))
        return resposta.json()["banco"]

    antes = banco("GET", 7)
    escrita = banco("POST", 7)
    logo_depois = banco("GET", 7)
    outra_pessoa = banco("GET", 8)
    relogio.agora += 4.9
    quase = banco("GET", 7)
    relogio.agora += 0.1
    passou = banco("GET", 7)
    cabecalho = banco("GET", 8, **{"X-Read-Replica": "false"})
    painel = banco("GET", 8, "/api/v1/painel?fresh=true")
    painel_antigo = banco("GET", 8, "/api/v1/painel")

    assert (antes, escrita, logo_depois, outra_pessoa) == (
        "replica",
        "primario",
        "primario",
        "replica",
    )
    assert (quase, passou) == ("primario", "replica")
    assert (cabecalho, painel, painel_antigo) == ("primario", "primario", "replica")
    assert escritas.expires == {}


def test_requests_without_a_user_leave_no_write_mark(monkeypatch, chave) -> None:
    relogio = _relogio()
    cliente, escritas = _cliente(monkeypatch, chave, relogio)

    sem_token = cliente.post("/api/v1/teste")
    da_extensao = cliente.post("/coleta", json={})

    assert (sem_token.status_code, da_extensao.status_code) == (401, 403)
    assert escritas.expires == {}


def test_the_router_runs_after_authentication_and_rls() -> None:
    camadas = [camada.cls for camada in main.app.user_middleware]

    assert (
        camadas.index(JWTAuthMiddleware)
        < camadas.index(RLSMiddleware)
        < camadas.index(RouterMiddleware)
    )


def test_process_write_marks_are_shared_by_every_request() -> None:
    router.get_recent_writes.cache_clear()
    try:
        assert router.get_recent_writes() is router.get_recent_writes()
    finally:
        router.get_recent_writes.cache_clear()


async def _parametros_de_conexao(motor):
    class ConexaoInterrompidaError(Exception):
        pass

    capturados = []

    def capturar(dialect, conn_rec, cargs, cparams):
        capturados.append(cparams)
        raise ConexaoInterrompidaError

    event.listen(motor.sync_engine, "do_connect", capturar)
    try:
        with pytest.raises(ConexaoInterrompidaError):
            async with motor.connect():
                pass
    finally:
        event.remove(motor.sync_engine, "do_connect", capturar)
    return capturados[0]


async def test_the_replica_engine_refuses_writes_and_both_engines_use_their_timeouts() -> None:
    settings = get_settings()

    primario = await _parametros_de_conexao(db_session.engine)
    replica = await _parametros_de_conexao(db_session.replica_engine)

    assert primario["server_settings"] == {
        "statement_timeout": str(settings.STATEMENT_TIMEOUT_PRIMARY)
    }
    assert replica["server_settings"] == {
        "statement_timeout": str(settings.STATEMENT_TIMEOUT_REPLICA),
        "default_transaction_read_only": "on",
    }
    assert replica["database"] == make_url(settings.DATABASE_URL_REPLICA).database
    assert primario["database"] == make_url(settings.DATABASE_URL).database


async def test_the_lag_check_runs_before_the_session_takes_its_connection(monkeypatch) -> None:
    ordem = []

    class Sessao:
        bind = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement, params=None):
            ordem.append("set_config")

    class Checagem:
        async def run(self, session):
            ordem.append("atraso")

    monkeypatch.setattr(db_session, "ReplicaSession", Sessao)
    monkeypatch.setattr(db_session, "replica_lag", Checagem())
    token = current_user_id.set("7")
    try:
        await _uma(db_session.get_db_replica())
    finally:
        current_user_id.reset(token)

    assert ordem == ["atraso", "set_config"]
