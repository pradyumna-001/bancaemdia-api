"""Real app, JWT and PostgreSQL: unit-value periods never rewrite bets and replay exactly (#210)."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
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
from bancaemdia.api.v1.unidade import LIMITE_CENTAVOS
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.seed import seed_canonical
from bancaemdia.db.session import get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot
from bancaemdia.main import app

pytestmark = pytest.mark.xdist_group("postgres")
APOSTA = {
    "casa": "betano",
    "odd": 1.95,
    "stake_unidades": 2.0,
    "evento": "Time A x Time B",
    "descricao": "Time A vence",
    "mercado_bruto": "resultado final",
    "freebet": False,
}


@dataclass
class Api:
    http: httpx.AsyncClient
    admin: AsyncEngine
    token: Callable[[int], str]
    novo: Callable[[], Awaitable[int]]

    async def call(self, method: str, url: str, uid: int, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token(uid)}", **kw.pop("headers", {})}
        return await self.http.request(method, url, headers=headers, **kw)

    async def definir(self, uid: int, valor: object, chave: str | None = None) -> httpx.Response:
        return await self.call(
            "POST",
            "/api/v1/unidade/vigencias",
            uid,
            json={"valor_centavos": valor},
            headers={"Idempotency-Key": chave or uuid4().hex},
        )

    async def vigencias(self, uid: int) -> list[Any]:
        async with self.admin.connect() as conn:
            return list(
                (
                    await conn.execute(
                        select(models.Unidade.__table__)
                        .where(models.Unidade.usuario_id == uid)
                        .order_by(models.Unidade.vigente_de)
                    )
                ).mappings()
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
        "unidade": {
            **jwt.algorithms.RSAAlgorithm.to_jwk(load_pem_public_key(public), as_dict=True),
            "kid": "unidade",
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
        return jwt.encode(claims, private, "RS256", headers={"kid": "unidade"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://api.test"
    ) as http:
        yield Api(http, engine_admin, token, novo_usuario)


async def _aposta(api: Api, uid: int) -> dict[str, Any]:
    response = await api.call("POST", "/api/v1/apostas", uid, json=APOSTA)
    assert response.status_code == 201, response.text
    async with api.admin.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(models.Aposta.__table__).where(
                        models.Aposta.usuario_id == uid,
                        models.Aposta.chave == response.json()["aposta"]["chave"],
                    )
                )
            )
            .mappings()
            .one()
        )
        unidade = await conn.scalar(
            select(models.Evento.payload_json["valor_unidade_centavos"].as_integer()).where(
                models.Evento.usuario_id == uid,
                models.Evento.aposta_chave == row["chave"],
                models.Evento.tipo == "APOSTA_CRIADA",
            )
        )
    return {**row, "valor_unidade_evento": unidade}


async def test_without_a_period_the_system_default_applies(api: Api) -> None:
    uid = await api.novo()
    response = await api.call("GET", "/api/v1/unidade", uid)
    assert response.status_code == 200
    body = response.json()
    assert body["vigente"] == {
        "id": None,
        "valor_centavos": 10_000,
        "origem": "padrao",
        "vigente_de": None,
        "vigente_ate": None,
        "vigente_de_local": None,
    }
    assert body["historico"] == [] and body["padrao_centavos"] == 10_000
    assert (
        body["fuso_horario"] == "America/Sao_Paulo" and body["limite_centavos"] == LIMITE_CENTAVOS
    )
    assert response.headers["cache-control"] == "private, no-store"


async def test_new_period_never_rewrites_existing_bets_and_applies_to_later_ones(api: Api) -> None:
    uid = await api.novo()
    antes = await _aposta(api, uid)
    assert antes["stake_centavos"] == 20_000 and antes["valor_unidade_evento"] == 10_000

    response = await api.definir(uid, 5_000)
    assert response.status_code == 201, response.text
    recibo = response.json()
    assert recibo["apostas_existentes_alteradas"] == 0
    assert (
        recibo["anterior"]["origem"] == "padrao" and recibo["anterior"]["valor_centavos"] == 10_000
    )
    assert recibo["vigencia"]["valor_centavos"] == 5_000
    assert recibo["vigencia"]["vigente_de"] == recibo["vale_a_partir_de"]
    assert recibo["vigencia"]["vigente_de_local"].endswith("-03:00")

    async with api.admin.connect() as conn:
        depois_da_troca = (
            await conn.execute(
                select(models.Aposta.stake_centavos, models.Aposta.valor_aposta_centavos).where(
                    models.Aposta.id == antes["id"]
                )
            )
        ).one()
    assert depois_da_troca == (antes["stake_centavos"], antes["valor_aposta_centavos"])
    nova = await _aposta(api, uid)
    assert nova["stake_centavos"] == 10_000 and nova["valor_unidade_evento"] == 5_000

    segunda = await api.definir(uid, 7_500)
    assert segunda.json()["anterior"]["valor_centavos"] == 5_000
    lida = (await api.call("GET", "/api/v1/unidade", uid)).json()
    assert lida["vigente"]["valor_centavos"] == 7_500 and lida["vigente"]["origem"] == "usuario"
    assert [item["valor_centavos"] for item in lida["historico"]] == [7_500, 5_000]
    assert lida["historico"][1]["vigente_ate"] == lida["historico"][0]["vigente_de"]


async def test_replay_returns_the_original_receipt_and_a_different_body_conflicts(
    api: Api,
) -> None:
    uid = await api.novo()
    chave = uuid4().hex
    primeira = await api.definir(uid, 5_000, chave)
    await api.definir(uid, 8_000)
    repetida = await api.definir(uid, 5_000, chave)
    assert repetida.status_code == 201 and repetida.json() == primeira.json()
    conflito = await api.definir(uid, 6_000, chave)
    assert conflito.status_code == 409 and conflito.json() == {"detail": "idempotency_key_conflict"}
    assert [row["valor_centavos"] for row in await api.vigencias(uid)] == [5_000, 8_000]

    recuperada = await api.call(
        "GET", "/api/v1/unidade/vigencias/intencao", uid, headers={"Idempotency-Key": chave}
    )
    assert recuperada.status_code == 200 and recuperada.json() == primeira.json()
    ausente = await api.call(
        "GET", "/api/v1/unidade/vigencias/intencao", uid, headers={"Idempotency-Key": "nunca"}
    )
    assert ausente.status_code == 404 and ausente.json() == {"detail": "intencao_nao_encontrada"}


async def test_periods_and_identities_are_private_to_each_user(api: Api) -> None:
    ana, bia = await api.novo(), await api.novo()
    chave = uuid4().hex
    assert (await api.definir(ana, 5_000, chave)).status_code == 201
    # A mesma chave de outra pessoa é outra intenção, nunca o recibo de Ana.
    assert (await api.definir(bia, 9_000, chave)).json()["vigencia"]["valor_centavos"] == 9_000
    leitura = (await api.call("GET", "/api/v1/unidade", bia)).json()
    assert [item["valor_centavos"] for item in leitura["historico"]] == [9_000]
    recuperada = await api.call(
        "GET", "/api/v1/unidade/vigencias/intencao", bia, headers={"Idempotency-Key": chave}
    )
    assert recuperada.json()["vigencia"]["valor_centavos"] == 9_000


async def test_read_only_account_reads_but_never_writes(api: Api) -> None:
    uid = await api.novo()
    chave = uuid4().hex
    assert (await api.definir(uid, 5_000, chave)).status_code == 201
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
        negada = await api.definir(uid, 6_000)
        assert negada.status_code == 402 and negada.json() == {"detail": "account_read_only"}
        assert (await api.call("GET", "/api/v1/unidade", uid)).status_code == 200
        recuperada = await api.call(
            "GET", "/api/v1/unidade/vigencias/intencao", uid, headers={"Idempotency-Key": chave}
        )
        assert recuperada.status_code == 200
        assert [row["valor_centavos"] for row in await api.vigencias(uid)] == [5_000]
    finally:
        async with api.admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:previous WHERE id=1"),
                {"previous": previous},
            )


async def test_concurrent_writes_converge_to_contiguous_periods(api: Api) -> None:
    uid = await api.novo()
    respostas = await asyncio.gather(*(api.definir(uid, valor) for valor in (4_000, 6_000)))
    assert [r.status_code for r in respostas] == [201, 201]
    linhas = await api.vigencias(uid)
    assert len(linhas) == 2 and linhas[0]["vigente_ate"] == linhas[1]["vigente_de"]
    assert linhas[1]["vigente_ate"] is None
    async with api.admin.connect() as conn:
        abertas = await conn.scalar(
            select(func.count())
            .select_from(models.Unidade)
            .where(models.Unidade.usuario_id == uid, models.Unidade.vigente_ate.is_(None))
        )
    assert abertas == 1


@pytest.mark.parametrize(
    "corpo,campo,codigo",
    [
        ({"valor_centavos": 100.5}, "valor_centavos", "inteiro"),
        ({"valor_centavos": 5000.0}, "valor_centavos", "inteiro"),
        ({"valor_centavos": "5000"}, "valor_centavos", "inteiro"),
        ({"valor_centavos": True}, "valor_centavos", "inteiro"),
        ({"valor_centavos": 0}, "valor_centavos", "positivo"),
        ({"valor_centavos": -1}, "valor_centavos", "positivo"),
        ({"valor_centavos": LIMITE_CENTAVOS + 1}, "valor_centavos", "acima_do_limite"),
        ({}, "valor_centavos", "obrigatorio"),
        ({"valor_centavos": 5000, "vigente_de": "2026-01-01"}, "vigente_de", "desconhecido"),
    ],
)
async def test_invalid_values_are_refused_with_a_code_per_field(
    api: Api, corpo: dict[str, object], campo: str, codigo: str
) -> None:
    uid = await api.novo()
    response = await api.call(
        "POST", "/api/v1/unidade/vigencias", uid, json=corpo, headers={"Idempotency-Key": "k"}
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "unidade_invalida",
        "campos": [{"campo": campo, "codigo": codigo}],
    }
    assert await api.vigencias(uid) == []


async def test_missing_identity_is_refused_before_any_write(api: Api) -> None:
    uid = await api.novo()
    response = await api.call(
        "POST", "/api/v1/unidade/vigencias", uid, json={"valor_centavos": 5_000}
    )
    assert response.status_code == 422
    assert response.json()["campos"] == [{"campo": "idempotency-key", "codigo": "obrigatorio"}]
    assert await api.vigencias(uid) == []
