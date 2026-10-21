"""Real app, JWT and PostgreSQL: site bankrolls, capital null versus zero, archival (#211)."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.api.v1.bancas import LIMITE_CENTAVOS
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.cli.refresh_painel import refresh_painel
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.seed import seed_canonical
from bancaemdia.db.session import get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot
from bancaemdia.main import app

pytestmark = pytest.mark.xdist_group("postgres")


@dataclass
class Api:
    http: httpx.AsyncClient
    admin: AsyncEngine
    token: Callable[[int], str]
    novo: Callable[[], Awaitable[int]]

    async def call(self, method: str, url: str, uid: int, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token(uid)}", **kw.pop("headers", {})}
        return await self.http.request(method, url, headers=headers, **kw)

    async def write(
        self, method: str, url: str, uid: int, corpo: Any = None, chave: str | None = None
    ) -> httpx.Response:
        kw: dict[str, Any] = {"headers": {"Idempotency-Key": chave or uuid4().hex}}
        if corpo is not None:
            kw["json"] = corpo
        return await self.call(method, url, uid, **kw)

    async def criar(self, uid: int, corpo: dict[str, Any]) -> dict[str, Any]:
        response = await self.write("POST", "/api/v1/bancas", uid, corpo)
        assert response.status_code == 201, response.text
        return dict(response.json())

    async def conta(self, uid: int, banca_id: int | None) -> int:
        async with self.admin.begin() as conn:
            casa = await conn.scalar(text("SELECT id FROM casas ORDER BY id LIMIT 1"))
            return int(
                await conn.scalar(
                    text(
                        "INSERT INTO contas_casa (usuario_id, casa_id, banca_id, apelido) "
                        "VALUES (:uid, :casa, :banca, :apelido) RETURNING id"
                    ),
                    {"uid": uid, "casa": casa, "banca": banca_id, "apelido": uuid4().hex[:8]},
                )
            )


@pytest.fixture
async def api(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Api]:
    async with engine_admin.begin() as conn:
        await seed_canonical(conn)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    cache = JWKSCache(None, "RS256")
    cache.keys = {
        "bancas": {
            **jwt.algorithms.RSAAlgorithm.to_jwk(load_pem_public_key(public), as_dict=True),
            "kid": "bancas",
        }
    }
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)

    async def sessions() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            if user := current_user_id.get():
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user)}
                )
            yield session

    for dependency in (get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot):
        monkeypatch.setitem(app.dependency_overrides, dependency, sessions)
    settings = get_settings()

    def token(uid: int) -> str:
        claims = {
            "sub": str(uid),
            "aud": settings.JWT_AUDIENCE,
            "iss": settings.JWT_ISSUER,
            "exp": int(time.time()) + 600,
        }
        return jwt.encode(claims, private, "RS256", headers={"kid": "bancas"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://api.test"
    ) as http:
        yield Api(http, engine_admin, token, novo_usuario)


async def test_create_replays_exactly_and_lists_active_bankrolls(api: Api) -> None:
    uid = await api.novo()
    chave = uuid4().hex
    corpo = {"nome": "  Principal  ", "saldo_inicial_centavos": 100_000}
    primeira = await api.write("POST", "/api/v1/bancas", uid, corpo, chave)
    assert primeira.status_code == 201
    banca = primeira.json()
    assert banca["nome"] == "Principal" and banca["estado"] == "ativa"
    assert banca["saldo_inicial_centavos"] == 100_000 and banca["contas"] == []
    repetida = await api.write("POST", "/api/v1/bancas", uid, corpo, chave)
    assert repetida.status_code == 201 and repetida.json() == banca
    outra = await api.write("POST", "/api/v1/bancas", uid, {"nome": "Outra"}, chave)
    assert outra.status_code == 409 and outra.json() == {"detail": "idempotency_key_conflict"}
    duplicada = await api.write("POST", "/api/v1/bancas", uid, {"nome": "Principal"})
    assert duplicada.status_code == 409 and duplicada.json() == {"detail": "nome_em_uso"}

    conta = await api.conta(uid, banca["id"])
    lista = (await api.call("GET", "/api/v1/bancas", uid)).json()["bancas"]
    assert [item["id"] for item in lista] == [banca["id"]]
    assert lista[0]["contas"][0]["id"] == conta
    recuperada = await api.call(
        "GET", "/api/v1/bancas/intencao", uid, headers={"Idempotency-Key": chave}
    )
    assert recuperada.json() == {"operacao": "CRIAR", "status_code": 201, "resposta": banca}
    ausente = await api.call(
        "GET", "/api/v1/bancas/intencao", uid, headers={"Idempotency-Key": "nunca"}
    )
    assert ausente.status_code == 404


async def test_capital_null_stays_unknown_and_zero_stays_zero_in_balance_and_analytics(
    api: Api,
) -> None:
    uid = await api.novo()
    sem = await api.criar(uid, {"nome": "Sem capital"})
    zero = await api.criar(uid, {"nome": "Capital zero", "saldo_inicial_centavos": 0})
    com = await api.criar(uid, {"nome": "Com capital", "saldo_inicial_centavos": 100_000})
    assert sem["saldo_inicial_centavos"] is None and zero["saldo_inicial_centavos"] == 0
    async with api.admin.begin() as conn:
        for banca in (sem, zero, com):
            conta = await api.conta(uid, banca["id"])
            await conn.execute(
                text(
                    "INSERT INTO apostas (usuario_id, chave, origem, banca_id, conta_casa_id, "
                    "data_aposta, data_jogo, stake_unidades, stake_centavos, "
                    "valor_aposta_centavos, odd, retorno_centavos, estado) VALUES (:uid, :chave, "
                    "'manual', :banca, :conta, :data, :data, 1, 10000, 10000, 2.0, 20000, 'GREEN')"
                ),
                {
                    "uid": uid,
                    "chave": f"banca-{uuid4().hex}",
                    "banca": banca["id"],
                    "conta": conta,
                    "data": datetime(2026, 10, 1, tzinfo=UTC),
                },
            )
    await refresh_painel(api.admin)

    saldo = (await api.call("GET", "/api/v1/caixa/saldo", uid)).json()
    iniciais = {item["id"]: item["saldo_inicial_centavos"] for item in saldo["bancas"]}
    assert iniciais == {sem["id"]: None, zero["id"]: 0, com["id"]: 100_000}

    analises = await api.call("GET", "/api/v1/painel/analises?periodo=all", uid)
    assert analises.status_code == 200, analises.text
    grupos = {item["chave"]: item for item in analises.json()["por_banca_progressao"]}
    assert grupos[str(sem["id"])]["capital_banca_centavos"] is None
    assert grupos[str(sem["id"])]["progressao"] is None
    assert grupos[str(zero["id"])]["progressao"] is None
    assert grupos[str(com["id"])]["progressao"] is not None

    removido = await api.write(
        "PATCH", f"/api/v1/bancas/{com['id']}", uid, {"saldo_inicial_centavos": None}
    )
    assert removido.status_code == 200 and removido.json()["saldo_inicial_centavos"] is None
    assert removido.json()["nome"] == "Com capital"


async def test_rename_archive_and_linking_rules(api: Api) -> None:
    uid = await api.novo()
    banca = await api.criar(uid, {"nome": "Antiga"})
    outra = await api.criar(uid, {"nome": "Ocupada"})
    renomeada = await api.write("PATCH", f"/api/v1/bancas/{banca['id']}", uid, {"nome": "Nova"})
    assert renomeada.status_code == 200 and renomeada.json()["nome"] == "Nova"
    conflito = await api.write("PATCH", f"/api/v1/bancas/{banca['id']}", uid, {"nome": "Ocupada"})
    assert conflito.status_code == 409 and conflito.json() == {"detail": "nome_em_uso"}

    conta = await api.conta(uid, banca["id"])
    com_conta = await api.write("POST", f"/api/v1/bancas/{banca['id']}/arquivar", uid)
    assert com_conta.status_code == 409 and com_conta.json() == {"detail": "banca_com_contas"}
    solta = await api.call(
        "PATCH", f"/api/v1/caixa/contas/{conta}/banca", uid, json={"banca_id": outra["id"]}
    )
    assert solta.status_code == 200
    chave = uuid4().hex
    arquivada = await api.write("POST", f"/api/v1/bancas/{banca['id']}/arquivar", uid, chave=chave)
    assert arquivada.status_code == 200 and arquivada.json()["estado"] == "arquivada"
    repetida = await api.write("POST", f"/api/v1/bancas/{banca['id']}/arquivar", uid, chave=chave)
    assert repetida.status_code == 200 and repetida.json() == arquivada.json()
    de_novo = await api.write("POST", f"/api/v1/bancas/{banca['id']}/arquivar", uid)
    assert de_novo.status_code == 409 and de_novo.json() == {"detail": "banca_arquivada"}
    editar = await api.write("PATCH", f"/api/v1/bancas/{banca['id']}", uid, {"nome": "X"})
    assert editar.status_code == 409 and editar.json() == {"detail": "banca_arquivada"}
    vinculo = await api.call(
        "PATCH", f"/api/v1/caixa/contas/{conta}/banca", uid, json={"banca_id": banca["id"]}
    )
    assert vinculo.status_code == 409
    # O nome continua identificando a história: arquivada não libera o nome.
    reuso = await api.write("POST", "/api/v1/bancas", uid, {"nome": "Nova"})
    assert reuso.status_code == 409 and reuso.json() == {"detail": "nome_em_uso"}
    lista = (await api.call("GET", "/api/v1/bancas", uid)).json()["bancas"]
    assert {item["id"]: item["estado"] for item in lista} == {
        banca["id"]: "arquivada",
        outra["id"]: "ativa",
    }


async def test_bankrolls_are_private_to_each_user(api: Api) -> None:
    ana, bia = await api.novo(), await api.novo()
    banca = await api.criar(ana, {"nome": "De Ana"})
    assert (await api.call("GET", "/api/v1/bancas", bia)).json() == {"bancas": []}
    alheia = await api.write("PATCH", f"/api/v1/bancas/{banca['id']}", bia, {"nome": "Minha"})
    assert alheia.status_code == 404 and alheia.json() == {"detail": "banca_nao_encontrada"}
    arquivar = await api.write("POST", f"/api/v1/bancas/{banca['id']}/arquivar", bia)
    assert arquivar.status_code == 404
    # O mesmo nome pertence a cada usuário separadamente.
    assert (await api.criar(bia, {"nome": "De Ana"}))["nome"] == "De Ana"


async def test_read_only_account_reads_but_never_writes(api: Api) -> None:
    uid = await api.novo()
    chave = uuid4().hex
    banca = (await api.write("POST", "/api/v1/bancas", uid, {"nome": "Antes"}, chave)).json()
    async with api.admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("SELECT billing_activate_rollout()"))
        await conn.execute(
            text(
                "UPDATE assinaturas SET status='EXPIRED', trial_confirmed=false WHERE usuario_id=:uid"
            ),
            {"uid": uid},
        )
    try:
        negada = await api.write("POST", "/api/v1/bancas", uid, {"nome": "Depois"})
        assert negada.status_code == 402 and negada.json() == {"detail": "account_read_only"}
        renomear = await api.write("PATCH", f"/api/v1/bancas/{banca['id']}", uid, {"nome": "Y"})
        assert renomear.status_code == 402
        assert (await api.call("GET", "/api/v1/bancas", uid)).status_code == 200
        recuperada = await api.call(
            "GET", "/api/v1/bancas/intencao", uid, headers={"Idempotency-Key": chave}
        )
        assert recuperada.status_code == 200
        async with api.admin.connect() as conn:
            nomes = list(
                await conn.scalars(select(models.Banca.nome).where(models.Banca.usuario_id == uid))
            )
            recibos = await conn.scalar(
                select(func.count())
                .select_from(models.BancaRequisicao)
                .where(models.BancaRequisicao.usuario_id == uid)
            )
        assert nomes == ["Antes"] and recibos == 1
    finally:
        async with api.admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:previous WHERE id=1"),
                {"previous": previous},
            )


@pytest.mark.parametrize(
    "metodo,caminho,corpo,campo,codigo",
    [
        ("POST", "", {}, "nome", "obrigatorio"),
        ("POST", "", {"nome": "   "}, "nome", "tamanho"),
        ("POST", "", {"nome": "x" * 81}, "nome", "tamanho"),
        (
            "POST",
            "",
            {"nome": "A", "saldo_inicial_centavos": 10.5},
            "saldo_inicial_centavos",
            "inteiro",
        ),
        (
            "POST",
            "",
            {"nome": "A", "saldo_inicial_centavos": "100"},
            "saldo_inicial_centavos",
            "inteiro",
        ),
        (
            "POST",
            "",
            {"nome": "A", "saldo_inicial_centavos": -1},
            "saldo_inicial_centavos",
            "nao_negativo",
        ),
        (
            "POST",
            "",
            {"nome": "A", "saldo_inicial_centavos": LIMITE_CENTAVOS + 1},
            "saldo_inicial_centavos",
            "acima_do_limite",
        ),
        ("POST", "", {"nome": "A", "modo": "conjunta"}, "modo", "desconhecido"),
        ("PATCH", "/1", {}, "corpo", "vazio"),
        ("PATCH", "/1", {"nome": None}, "nome", "obrigatorio"),
    ],
)
async def test_invalid_requests_are_refused_with_a_code_per_field(
    api: Api, metodo: str, caminho: str, corpo: dict[str, Any], campo: str, codigo: str
) -> None:
    uid = await api.novo()
    response = await api.write(metodo, f"/api/v1/bancas{caminho}", uid, corpo)
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": "banca_invalida",
        "campos": [{"campo": campo, "codigo": codigo}],
    }
