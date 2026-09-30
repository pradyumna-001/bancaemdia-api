import json
import secrets
import time

import httpx
import jwt
import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from bancaemdia.auth.identity_config import IdentitySettings
from bancaemdia.auth.identity_crypto import IdentityKeys
from bancaemdia.auth.identity_service import destination
from bancaemdia.auth.jwt import InvalidTokenError, verify_token
from bancaemdia.auth.oidc import IdentityError, OIDCClient
from bancaemdia.config import get_settings
from tests.identity.support import key_settings


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example",
        "//evil.example",
        "/%2fevil.example",
        "/\\evil",
        "/%0d%0aX",
        "/auth/callback",
        "/a#fragment",
        "relative",
        "/" + "x" * 2048,
    ],
)
def test_return_destination_never_leaves_the_frontend(value):
    with pytest.raises(IdentityError, match="invalid_destination"):
        destination(value)


def test_internal_destination_keeps_query():
    assert destination("/apostas?desde=2026-09-29") == "/apostas?desde=2026-09-29"


@pytest.mark.parametrize(
    "changes",
    [
        {"AUTH_PUBLIC_URL": "http://api.example.org"},
        {"AUTH_FRONTEND_ORIGIN": "https://other.example.org"},
        {"AUTH_FRONTEND_ORIGIN": "https://app.example.org/path"},
        {"AUTH_COOKIE_SECURE": False},
        {"OIDC_SCOPES": "openid"},
        {"OIDC_ISSUER": "https://user:password@issuer.example.org"},
    ],
)
def test_enabled_configuration_rejects_unsafe_deployment(tmp_path, changes):
    settings = key_settings(tmp_path)
    data = settings.model_dump()
    data.update(
        AUTH_ENABLED=True,
        AUTH_DATABASE_URL="postgresql+asyncpg://test@localhost/db",
        AUTH_PUBLIC_URL="https://app.example.org",
        AUTH_FRONTEND_ORIGIN="https://app.example.org",
        OIDC_ISSUER="https://issuer.example.org",
        OIDC_CLIENT_ID="test",
    )
    data.update(changes)
    with pytest.raises(ValidationError):
        IdentitySettings(**data)


async def test_internal_jwt_reuses_validator_and_contains_only_internal_subject(tmp_path):
    keys = IdentityKeys(key_settings(tmp_path))
    token = keys.issue(73, "session-id", 60, 3)
    assert await verify_token(token, keys.cache) == 73
    claims = jwt.decode(
        token, keys.private.public_key(), algorithms=["RS256"], audience=get_settings().JWT_AUDIENCE
    )
    assert claims["sub"] == "73" and claims["sid"] == "session-id" and claims["ver"] == 3
    assert "email" not in claims
    assert all(not {"d", "p", "q", "dp", "dq", "qi"}.intersection(key) for key in keys.jwks["keys"])
    assert set(keys.jwks["keys"][0]["key_ops"]) == {"verify"}
    with pytest.raises(InvalidTokenError):
        await verify_token(keys.issue(73, "session-id", -1), keys.cache)


def test_ciphertext_cannot_be_swapped_or_modified_and_csrf_is_cookie_bound(tmp_path):
    keys = IdentityKeys(key_settings(tmp_path))
    encoded = keys.seal("provider-refresh-secret", "session:one")
    assert "provider-refresh-secret" not in encoded
    assert keys.open(encoded, "session:one") == "provider-refresh-secret"
    with pytest.raises(InvalidTag):
        keys.open(encoded, "session:two")
    assert keys.csrf("one") != keys.csrf("two")
    assert keys.seal("same", "session:one") != keys.seal("same", "session:one")


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://untrusted.example.org"},
        {"aud": "other"},
        {"exp": 1},
        {"sub": ""},
        {"nonce": "other"},
        {"iat": True},
        {"azp": "other"},
        {"email_verified": False},
        {"email_verified": "true"},
    ],
)
async def test_external_identity_validation_is_auxiliary_not_acceptance(tmp_path, change):
    settings = key_settings(
        tmp_path, OIDC_ISSUER="https://issuer.example.org", OIDC_CLIENT_ID="test"
    )
    keys = IdentityKeys(settings)
    client = OIDCClient(settings)
    client.document, client.document_at, client.cache = {}, time.monotonic(), keys.cache
    claims = {
        "iss": settings.OIDC_ISSUER,
        "aud": "test",
        "sub": "external-uuid",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "nonce": "nonce",
        "email": "u@example.org",
        "email_verified": True,
        **change,
    }
    token = jwt.encode(claims, keys.private, algorithm="RS256", headers={"kid": keys.kid})
    with pytest.raises(IdentityError):
        await client.identity(token, "nonce")


async def test_unknown_signing_key_is_401_and_unavailable_key_set_is_503(tmp_path):
    settings = key_settings(
        tmp_path, OIDC_ISSUER="https://issuer.example.org", OIDC_CLIENT_ID="test"
    )
    keys = IdentityKeys(settings)
    client = OIDCClient(settings)
    client.document, client.document_at, client.cache = {}, time.monotonic(), keys.cache
    token = jwt.encode({"sub": "x"}, keys.private, algorithm="RS256", headers={"kid": "unknown"})
    with pytest.raises(IdentityError) as reason:
        await client.identity(token, None)
    assert reason.value.status == 401
    client.cache.keys = {}
    with pytest.raises(IdentityError) as reason:
        await client.identity(token, None)
    assert reason.value.status == 503


async def test_disabled_session_routes_cannot_simulate_login():
    from bancaemdia.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("start", "session", "jwks"):
            response = await client.get("/auth/" + path)
            assert response.status_code == 503
            assert response.json()["code"] == "identity_not_configured"
            assert response.headers["cache-control"] == "private, no-store"


def test_retained_encryption_keys_allow_rotation(tmp_path):
    settings = key_settings(tmp_path)
    original = IdentityKeys(settings)
    ciphertext = original.seal("refresh", "session:one")
    path = settings.AUTH_ENCRYPTION_KEYS_FILE
    data = json.loads(path.read_text())
    import base64

    data["active"] = "next"
    data["keys"]["next"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    path.write_text(json.dumps(data))
    assert IdentityKeys(settings).open(ciphertext, "session:one") == "refresh"


def test_existing_api_error_body_is_preserved_and_auth_code_is_an_additive_header():
    from bancaemdia.api.contracts import ErrorResponse
    from bancaemdia.api.identity import AuthFailure, failure

    reason = IdentityError("access_expired")
    legacy = failure(reason, legacy=True)
    ErrorResponse.model_validate_json(legacy.body)
    assert json.loads(legacy.body) == {"detail": "Identity request could not be completed"}
    assert legacy.headers["x-auth-error"] == "access_expired"
    AuthFailure.model_validate_json(failure(reason).body)


async def test_identity_query_validation_does_not_reflect_authorization_codes():
    from bancaemdia.main import app

    code = "SECRET-CODE-" + "x" * 4097
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/auth/callback", params={"state": "state", "code": code})
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert "SECRET-CODE" not in response.text and "input" not in response.json()
