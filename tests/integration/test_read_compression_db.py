"""Real app, JWT and PostgreSQL: allowlisted reads compress, secrets and mutations never do."""

import gzip
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.cli.refresh_painel import refresh_painel
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.seed import seed_canonical
from bancaemdia.db.session import get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot
from bancaemdia.main import app
from bancaemdia.middleware.compression import COMPRESSIBLE_READS

pytestmark = pytest.mark.xdist_group("postgres")
TOTAL_BETS = 600
# Mudam a cada pedido (relógio, idade da visão e cursores HMAC emitidos de novo a cada resposta);
# o restante da resposta tem de ser idêntico.
VOLATILE = {
    "respondido_em",
    "idade_mv_segundos",
    "replica_atraso_segundos",
    "snapshot_em",
    "observado_em",
    "fronteira_iniciada_em",
    "proximo_cursor",
    "anterior_cursor",
    "next_cursor",
}


async def _seed(engine: AsyncEngine, usuario_id: int) -> None:
    prefix = uuid4().hex
    async with engine.begin() as conn:
        await seed_canonical(conn)
        casa = await conn.scalar(text("SELECT id FROM casas ORDER BY id LIMIT 1"))
        tipsters = list(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tipsters (nome) SELECT :p || ' tipster ' || g "
                        "FROM generate_series(1, 12) g RETURNING id"
                    ),
                    {"p": prefix},
                )
            ).scalars()
        )
        # Os mercados canônicos já existem: o catálogo compartilhado tem contagem exata em test_rls.
        mercados = list(
            (await conn.execute(text("SELECT id FROM mercados ORDER BY id LIMIT 12"))).scalars()
        )
        banca = await conn.scalar(
            text(
                "INSERT INTO bancas (usuario_id, nome, saldo_inicial_centavos) "
                "VALUES (:uid, :nome, 100000) RETURNING id"
            ),
            {"uid": usuario_id, "nome": f"Compressão {prefix}"},
        )
        conta = await conn.scalar(
            text(
                "INSERT INTO contas_casa (usuario_id, casa_id, banca_id, apelido) "
                "VALUES (:uid, :casa, :banca, 'compressao') RETURNING id"
            ),
            {"uid": usuario_id, "casa": casa, "banca": banca},
        )
        values = {
            "uid": usuario_id,
            "prefix": prefix,
            "base": datetime(2026, 10, 10, tzinfo=UTC),
            "banca": banca,
            "conta": conta,
            "tipsters": tipsters,
            "mercados": mercados,
        }
        key = "'compressao-' || :prefix || '-' || g::text"
        data = "CAST(:base AS timestamptz) - make_interval(days => g % 90, hours => g % 24)"
        state = "CASE g % 4 WHEN 0 THEN 'GREEN' WHEN 1 THEN 'RED' WHEN 2 THEN 'PENDENTE' ELSE 'CASHOUT' END"
        returned = "CASE g % 4 WHEN 0 THEN 20000 WHEN 1 THEN 0 WHEN 2 THEN NULL ELSE 7000 END"
        tipster = "(CAST(:tipsters AS bigint[]))[1 + g % 12]"
        mercado = "(CAST(:mercados AS bigint[]))[1 + g % 12]"
        await conn.execute(
            text(
                "INSERT INTO apostas (usuario_id, chave, origem, banca_id, conta_casa_id, tipster_id, "
                "mercado_id, data_aposta, data_jogo, stake_unidades, stake_centavos, "
                "valor_aposta_centavos, odd, retorno_centavos, estado) "
                f"SELECT :uid, {key}, 'manual', :banca, :conta, {tipster}, {mercado}, {data}, {data}, "
                f"1, 10000, 10000, 2.0, {returned}, {state} FROM generate_series(1, {TOTAL_BETS}) g"
            ),
            values,
        )
        await conn.execute(
            text(
                "INSERT INTO eventos (usuario_id, tipo, fonte, payload_json, aposta_chave) "
                "SELECT :uid, 'APOSTA_CRIADA', 'manual', jsonb_build_object("
                "'origem', 'manual', 'stake_unidades', 1, 'valor_unidade_centavos', 10000, "
                f"'odd', 2.0, 'data_aposta', {data}, 'data_jogo', {data}, "
                "'conta_casa_id', CAST(:conta AS bigint), 'conta_casa_ref', CAST(:conta AS bigint), "
                "'conta_referencia_explicita', true, "
                f"'tipster_id', {tipster}, 'mercado_id', {mercado}, "
                "'evento', 'Série ' || g || ' · Time da casa contra Visitante', "
                "'descricao', 'Total de gols · seleção com descrição extensa'), "
                f"{key} FROM generate_series(1, {TOTAL_BETS}) g"
            ),
            values,
        )
    await refresh_painel(engine)


@pytest.fixture
async def client(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    usuario_id = await novo_usuario()
    await _seed(engine_admin, usuario_id)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    cache = JWKSCache(None, "RS256")
    cache.keys = {
        "compression": {
            **jwt.algorithms.RSAAlgorithm.to_jwk(load_pem_public_key(public), as_dict=True),
            "kid": "compression",
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
    token = jwt.encode(
        {
            "sub": str(usuario_id),
            "aud": settings.JWT_AUDIENCE,
            "iss": settings.JWT_ISSUER,
            "exp": int(time.time()) + 600,
        },
        private,
        "RS256",
        headers={"kid": "compression"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        # A Coleta recusa credencial fora de HTTPS; o mesmo cliente serve para todas as rotas.
        base_url="https://api.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        # Cada pedido declara a própria codificação; o padrão do httpx esconderia o caso sem header.
        del http.headers["accept-encoding"]
        yield http


async def _raw(client: httpx.AsyncClient, method: str, url: str, accept: str | None, **kw: Any):
    headers = {} if accept is None else {"Accept-Encoding": accept}
    async with client.stream(method, url, headers=headers, **kw) as response:
        body = b"".join([chunk async for chunk in response.aiter_raw()])
    return response, body


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


MAIN_READS = [
    "/api/v1/painel/analises?periodo=all",
    "/api/v1/painel?periodo=all",
    "/api/v1/apostas?page_size=100",
]
# Leituras dos PRs #184/#189/#199: entram sozinhas quando a rota existe na árvore integrada, sem
# edição na reconciliação. Na main a rota não existe, então não há caso a pular.
INTEGRATED_READS = {
    "/api/v1/resultados/fila": "/api/v1/resultados/fila",
    "/api/v1/painel/filtrado": "/api/v1/painel/filtrado?periodo=all",
    "/api/v1/painel/filtrado/metricas": "/api/v1/painel/filtrado/metricas?periodo=all",
    "/api/v1/painel/filtrado/diario": "/api/v1/painel/filtrado/diario?periodo=all",
    "/api/v1/painel/filtrado/grupos": "/api/v1/painel/filtrado/grupos?periodo=all",
}
# O snapshot versionado, que o teste de contrato mantém igual ao app. Gerar `app.openapi()` aqui
# deixaria em cache, na coleta, o schema que esse mesmo teste compara (medido na CI).
ROUTES = set(
    json.loads(
        (Path(__file__).parents[1] / "contract" / "schemas" / "openapi.json").read_text(
            encoding="utf-8"
        )
    )["paths"]
)


def test_every_registered_allowlisted_read_has_a_real_case() -> None:
    assert set(INTEGRATED_READS) | {url.split("?")[0] for url in MAIN_READS} == COMPRESSIBLE_READS
    assert {url.split("?")[0] for url in MAIN_READS} <= ROUTES


@pytest.mark.parametrize(
    "url", MAIN_READS + [url for path, url in INTEGRATED_READS.items() if path in ROUTES]
)
async def test_allowlisted_read_compresses_without_changing_the_json(
    client: httpx.AsyncClient, url: str, record_property: Callable[[str, object], None]
) -> None:
    plain, plain_body = await _raw(client, "GET", url, None)
    packed, packed_body = await _raw(client, "GET", url, "gzip, deflate, br")
    assert plain.status_code == packed.status_code == 200, plain_body[:300]
    assert "content-encoding" not in plain.headers
    assert packed.headers["content-encoding"] == "gzip"
    # A pilha entrega o corpo em partes; o gzip sai em chunks, sem Content-Length.
    assert len(packed_body) < len(plain_body)
    for response in (plain, packed):
        assert "Accept-Encoding" in response.headers["vary"]
        assert response.headers["content-type"].startswith("application/json")
    unpacked = gzip.decompress(packed_body)
    assert _stable(json.loads(unpacked)) == _stable(json.loads(plain_body))
    if "/api/v1/apostas" in url:
        # A lista não tem campo de relógio: os bytes são exatamente os mesmos.
        assert unpacked == plain_body
    record_property(f"bytes_plain {url}", len(plain_body))
    record_property(f"bytes_gzip {url}", len(packed_body))


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "/auth/session"),
        ("POST", "/auth/refresh"),
        ("POST", "/api/v1/telegram/link-codes"),
        ("POST", "/api/v1/coleta/pairing-codes"),
        ("GET", "/api/v1/telegram/link"),
        ("GET", "/api/v1/coleta/installations"),
        ("GET", "/api/v1/painel/export?periodo=all"),
    ],
)
async def test_identity_codes_credentials_and_exports_never_compress(
    client: httpx.AsyncClient, method: str, url: str
) -> None:
    response, body = await _raw(client, method, url, "gzip")
    assert "content-encoding" not in response.headers, (url, response.status_code)
    assert "accept-encoding" not in response.headers.get("vary", "").lower()
    if url.endswith("link-codes"):
        assert response.status_code == 201 and json.loads(body)["code"]
    if url.endswith("pairing-codes"):
        assert response.status_code == 201 and json.loads(body)["codigo"]


async def test_mutation_on_an_allowlisted_path_never_compresses(client: httpx.AsyncClient) -> None:
    payload = {
        "origem": "manual",
        "stake_unidades": 1,
        "valor_unidade_centavos": 10000,
        "odd": 2.0,
        "evento": "Mutação " + "x" * 2000,
    }
    response, body = await _raw(client, "POST", "/api/v1/apostas", "gzip", json=payload)
    assert "content-encoding" not in response.headers, response.status_code
    assert "accept-encoding" not in response.headers.get("vary", "").lower()
    assert len(body) > 0
