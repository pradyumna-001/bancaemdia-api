"""Required acceptance: real PostgreSQL, isolated operators and synthetic identities."""

import os
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db, get_db_primary
from bancaemdia.main import app


@pytest.fixture(scope="session", autouse=True)
def mandatory_postgres():
    assert os.environ.get("TEST_DATABASE_URL"), (
        "Catalog acceptance requires real disposable PostgreSQL"
    )


@pytest.fixture
async def catalog_system(engine_app, engine_admin, novo_usuario, como, monkeypatch):
    uid, other = await novo_usuario(), await novo_usuario()
    async with engine_admin.begin() as conn:
        await conn.execute(
            text("INSERT INTO catalogo_operadores(usuario_id) VALUES (:uid)"), {"uid": uid}
        )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cache = JWKSCache(None, "RS256")
    cache.keys = {
        "catalog-test": {
            **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
            "kid": "catalog-test",
        }
    }
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)
    private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())

    def headers(user=uid):
        settings = get_settings()
        token = jwt.encode(
            {
                "sub": str(user),
                "aud": settings.JWT_AUDIENCE,
                "iss": settings.JWT_ISSUER,
                "exp": int(time.time()) + 300,
            },
            private,
            algorithm="RS256",
            headers={"kid": "catalog-test"},
        )
        return {"Authorization": "Bearer " + token}

    async def sessions():
        async with AsyncSession(engine_app, expire_on_commit=False) as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db_primary, sessions)
    monkeypatch.setitem(app.dependency_overrides, get_db, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(f"192.0.2.{uid % 250 + 1}", 113)),
        base_url="https://api.test",
    ) as client:
        yield SimpleNamespace(
            user=uid,
            other=other,
            engine=engine_app,
            admin=engine_admin,
            como=como,
            http=client,
            headers=headers,
            brand="CATALOG-" + uuid4().hex,
            environment=get_settings().APP_ENV,
        )
