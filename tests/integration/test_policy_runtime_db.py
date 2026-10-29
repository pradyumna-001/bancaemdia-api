"""The published policy is the runtime gate: a READ_ONLY account gets 402 exactly where declared."""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
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

from bancaemdia.api.policies import EXTENSION, FORA_DO_USUARIO
from bancaemdia.api.v1 import sheet_import
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot
from bancaemdia.main import app

pytestmark = pytest.mark.xdist_group("postgres")
SCHEMA = json.loads(
    (Path(__file__).parents[1] / "contract" / "schemas" / "openapi.json").read_text(
        encoding="utf-8"
    )
)
DO_USUARIO = [
    (method, path, operation)
    for path, item in SCHEMA["paths"].items()
    for method, operation in item.items()
    if method in ("get", "post", "put", "patch", "delete") and path not in FORA_DO_USUARIO
]


def _url(path: str, operation: dict[str, Any]) -> str:
    for parametro in operation.get("parameters", []):
        if parametro["in"] != "path":
            continue
        formato = parametro["schema"].get("format")
        valor = str(uuid4()) if formato == "uuid" else "1"
        path = path.replace("{" + parametro["name"] + "}", valor)
    return path


@pytest.fixture
async def read_only(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable[[], Awaitable[httpx.AsyncClient]]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    cache = JWKSCache(None, "RS256")
    cache.keys = {
        "policy": {
            **jwt.algorithms.RSAAlgorithm.to_jwk(load_pem_public_key(public), as_dict=True),
            "kid": "policy",
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
    # A planilha abre a própria sessão SERIALIZABLE pelo engine do módulo, fora de get_db.
    monkeypatch.setattr(sheet_import, "engine", engine_app)
    settings = get_settings()
    async with engine_admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("SELECT billing_activate_rollout()"))
    clients: list[httpx.AsyncClient] = []

    async def criar() -> httpx.AsyncClient:
        # Uma conta nova por operação: excluir a conta ou parear não afeta a próxima chamada.
        uid = await novo_usuario()
        async with engine_admin.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE assinaturas SET status='EXPIRED', trial_confirmed=false "
                    "WHERE usuario_id=:uid"
                ),
                {"uid": uid},
            )
        claims = {
            "sub": str(uid),
            "aud": settings.JWT_AUDIENCE,
            "iss": settings.JWT_ISSUER,
            "exp": int(time.time()) + 600,
        }
        token = jwt.encode(claims, private, "RS256", headers={"kid": "policy"})
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="https://api.test",
            headers={"Authorization": f"Bearer {token}"},
        )
        clients.append(client)
        return client

    try:
        yield criar
    finally:
        for client in clients:
            await client.aclose()
        async with engine_admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:previous WHERE id=1"),
                {"previous": previous},
            )


@pytest.mark.parametrize(("method", "path", "operation"), DO_USUARIO)
async def test_read_only_account_is_refused_exactly_where_the_policy_says(
    read_only: Callable[[], Awaitable[httpx.AsyncClient]],
    method: str,
    path: str,
    operation: dict[str, Any],
) -> None:
    client = await read_only()
    kw: dict[str, Any] = {"headers": {"Idempotency-Key": uuid4().hex}}
    if operation.get("requestBody"):
        kw["json"] = {}
    response = await client.request(method.upper(), _url(path, operation), **kw)
    so_escrita = operation[EXTENSION]["access_modes"] == ["FULL_WRITE"]
    assert (response.status_code == 402) == so_escrita, (response.status_code, response.text[:200])
    if so_escrita:
        assert response.json() == {"detail": "account_read_only"}
    assert response.status_code != 500, response.text[:200]
