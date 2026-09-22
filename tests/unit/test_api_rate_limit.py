from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.responses import Response

from bancaemdia import main
from bancaemdia.api.deps import get_current_user
from bancaemdia.api.v1 import upload
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.middleware import JWTAuthMiddleware
from bancaemdia.config import Settings, get_settings
from bancaemdia.db.session import get_db
from bancaemdia.middleware import rate_limit
from bancaemdia.middleware.rate_limit import (
    API_POLICY,
    AUTH_POLICY,
    COLETA_POLICY,
    UPLOAD_POLICY,
    AuthRateLimitMiddleware,
    RateLimitMiddleware,
    coleta_token_key,
    policy_for,
    reset_rate_limiters,
    user_key,
)
from bancaemdia.observability.logging import UserLogContextMiddleware


class _TestIdentityMiddleware(BaseHTTPMiddleware):
    """Stand in for JWT verification without involving a database or signing keys."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/api/v1/") and request.url.path not in {"/api/v1/coleta"}:
            user = request.headers.get("X-Test-User")
            if user is None:
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            request.state.usuario_id = int(user)
        return await call_next(request)


def _app() -> FastAPI:
    app = FastAPI()

    @app.api_route("/api/v1/one", methods=["GET", "POST"])
    def api_one() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/two")
    async def api_two() -> dict[str, bool]:
        return {"ok": True}

    @app.api_route("/api/v1/upload", methods=["GET", "POST"])
    def api_upload() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/coleta")
    @app.post("/coleta")
    async def api_coleta() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/auth/login")
    async def auth_login() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/auth/refresh")
    async def auth_refresh() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # Registration order is the reverse of execution order. Identity must run first so the
    # limiter can only use a user id that authentication has validated.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(_TestIdentityMiddleware)
    app.add_middleware(AuthRateLimitMiddleware)
    return app


@pytest.fixture(autouse=True)
def _isolated_limits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("API_RATE_LIMIT", "2/minute")
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    monkeypatch.setenv("UPLOAD_RATE_LIMIT", "1/minute")
    monkeypatch.setenv("COLETA_RATE_LIMIT", "2/minute")
    monkeypatch.setenv("COLETA_IP_RATE_LIMIT", "1000/minute")
    get_settings.cache_clear()
    reset_rate_limiters()
    yield
    reset_rate_limiters()
    get_settings.cache_clear()


def _request(
    path: str,
    *,
    method: str = "GET",
    root_path: str = "",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    client: tuple[str, int] = ("127.0.0.1", 1234),
) -> Request:
    return Request({
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": root_path,
        "query_string": b"",
        "headers": list(headers),
        "client": client,
        "server": ("testserver", 80),
    })


def _assert_rate_headers(response: Response, *, limit: int, remaining: int) -> None:
    assert response.headers["X-RateLimit-Limit"] == str(limit)
    assert response.headers["X-RateLimit-Remaining"] == str(remaining)
    assert float(response.headers["X-RateLimit-Reset"]) > time.time()


def test_rate_limit_defaults_match_the_public_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("API_RATE_LIMIT", "AUTH_RATE_LIMIT", "UPLOAD_RATE_LIMIT"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.API_RATE_LIMIT == "100/minute"
    assert settings.AUTH_RATE_LIMIT == "20/minute"
    assert settings.UPLOAD_RATE_LIMIT == "1/5minutes"
    assert settings.UPLOAD_MAX_BYTES == 50 * 1024 * 1024


def test_redis_backend_has_bounded_network_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_STORAGE", "redis://rate-limit.test:6379/4")
    monkeypatch.setenv("RATE_LIMIT_STORAGE_TIMEOUT_SECONDS", "0.25")
    get_settings.cache_clear()

    limiter = rate_limit._limiter(lambda: "1/minute", user_key, "timeout-test")
    options = limiter._storage.storage.connection_pool.connection_kwargs

    assert options["socket_connect_timeout"] == pytest.approx(0.25)
    assert options["socket_timeout"] == pytest.approx(0.25)


def test_storage_failure_reports_each_fallback_transition_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATE_LIMIT_STORAGE", "redis://rate-limit.test:6379/4")
    get_settings.cache_clear()
    limiter = rate_limit._limiter(lambda: "2/minute", user_key, "api")
    labels = {"policy": "api", "storage_backend": "redis"}
    before = REGISTRY.get_sample_value("rate_limiter_fallback_total", labels) or 0.0

    def storage_unavailable(*args: object, **kwargs: object) -> bool:
        raise ConnectionError("rate-limit storage unavailable")

    monkeypatch.setattr(limiter._limiter, "hit", storage_unavailable)
    monkeypatch.setattr(limiter._storage, "check", lambda: False)

    def limited_request(usuario_id: int) -> Request:
        request = _request("/api/v1/one")
        request.state.usuario_id = usuario_id
        return request

    with structlog.testing.capture_logs() as logs:
        limiter._check_request_limit(limited_request(7), rate_limit._api_scope, True)
        limiter._check_request_limit(limited_request(7), rate_limit._api_scope, True)

        # Model a successful backend probe. A later outage is a new transition and must be
        # observable again, while requests during the same outage must not increment the signal.
        limiter._storage_dead = False
        limiter._check_request_limit(limited_request(8), rate_limit._api_scope, True)

    warnings = [entry for entry in logs if entry["event"] == "rate_limiter_fallback"]
    assert warnings == [
        {
            "event": "rate_limiter_fallback",
            "policy": "api",
            "storage_backend": "redis",
            "fallback_backend": "memory",
            "log_level": "warning",
        },
        {
            "event": "rate_limiter_fallback",
            "policy": "api",
            "storage_backend": "redis",
            "fallback_backend": "memory",
            "log_level": "warning",
        },
    ]
    assert REGISTRY.get_sample_value("rate_limiter_fallback_total", labels) == pytest.approx(
        before + 2
    )


def test_user_key_uses_only_authenticated_state_and_never_the_bearer_token() -> None:
    first = _request("/api/v1/one", headers=((b"authorization", b"Bearer secret-token-one"),))
    second = _request("/api/v1/one", headers=((b"authorization", b"Bearer secret-token-two"),))
    first.state.usuario_id = 7
    second.state.usuario_id = 7

    assert user_key(first) == user_key(second) == "user:7"
    assert "secret-token" not in user_key(first)


def test_anonymous_auth_keys_are_isolated_by_the_direct_client_not_forwarded_headers() -> None:
    first = _request(
        "/auth/login",
        client=("192.0.2.10", 1234),
        headers=((b"x-forwarded-for", b"203.0.113.99"),),
    )
    same_host = _request("/auth/refresh", client=("192.0.2.10", 9999))
    another_host = _request("/auth/login", client=("192.0.2.11", 1234))

    assert user_key(first) == user_key(same_host)
    assert user_key(another_host) != user_key(first)
    assert "192.0.2.10" not in user_key(first)
    assert "203.0.113.99" not in user_key(first)


def test_forwarded_client_is_used_only_from_an_explicitly_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_CIDRS", "192.0.2.0/24")
    get_settings.cache_clear()
    request = _request(
        "/auth/login",
        client=("192.0.2.10", 1234),
        headers=((b"x-forwarded-for", b"198.51.100.25, 192.0.2.11"),),
    )

    direct_client = _request("/auth/login", client=("198.51.100.25", 1234))

    assert user_key(request) == user_key(direct_client)


def test_extension_keys_are_stable_isolated_hashes_without_the_raw_token() -> None:
    token = "extension-secret-token"
    first = _request("/coleta", method="POST", headers=((b"x-coleta-token", token.encode()),))
    alias = _request(
        "/api/v1/coleta", method="POST", headers=((b"x-coleta-token", token.encode()),)
    )
    another = _request("/coleta", method="POST", headers=((b"x-coleta-token", b"another-token"),))

    digest = hmac.new(
        b"TEST_COLETA_TOKEN_SECRET",
        f"rate-limit:coleta-token:{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    expected = f"extension:{digest}"
    assert coleta_token_key(first) == coleta_token_key(alias) == expected
    assert coleta_token_key(another) != expected
    assert token not in expected


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/coleta", "POST", COLETA_POLICY),
        ("/api/v1/coleta", "POST", COLETA_POLICY),
        ("/coleta", "GET", None),
        ("/api/v1/coleta", "OPTIONS", None),
        ("/api/v1/upload", "POST", UPLOAD_POLICY),
        ("/api/v1/upload", "GET", API_POLICY),
        ("/api/v1", "GET", API_POLICY),
        ("/api/v1/apostas", "GET", API_POLICY),
        ("/auth", "POST", AUTH_POLICY),
        ("/auth/login", "POST", AUTH_POLICY),
        ("/health", "GET", None),
        ("/metrics", "GET", None),
        ("/api/v10/apostas", "GET", None),
        ("/authorize", "POST", None),
        ("/api/v1/coleta-extra", "POST", API_POLICY),
    ],
)
def test_policy_selection_has_exact_prefixes_and_specific_routes_win(
    path: str, method: str, expected: object
) -> None:
    assert policy_for(_request(path, method=method)) is expected


def test_policy_selection_matches_paths_behind_a_root_path() -> None:
    request = _request("/prefix/api/v1/upload", method="POST", root_path="/prefix")

    assert policy_for(request) is UPLOAD_POLICY


def test_general_api_quota_is_shared_across_routes_for_one_user() -> None:
    client = TestClient(_app())
    headers = {"X-Test-User": "7"}

    first = client.get("/api/v1/one", headers=headers)
    second = client.get("/api/v1/two", headers=headers)
    exceeded = client.post("/api/v1/one", headers=headers)

    assert [first.status_code, second.status_code, exceeded.status_code] == [200, 200, 429]
    _assert_rate_headers(first, limit=2, remaining=1)
    _assert_rate_headers(second, limit=2, remaining=0)
    _assert_rate_headers(exceeded, limit=2, remaining=0)
    assert exceeded.json() == {
        "error": "rate_limited",
        "retry_after": int(exceeded.headers["Retry-After"]),
    }
    assert 1 <= int(exceeded.headers["Retry-After"]) <= 60


def test_load_of_200_requests_accepts_100_then_rate_limits_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_RATE_LIMIT", "100/minute")
    get_settings.cache_clear()
    reset_rate_limiters()
    client = TestClient(_app())
    headers = {"X-Test-User": "7"}

    responses = [client.get("/api/v1/one", headers=headers) for _ in range(200)]
    accepted, exceeded = responses[:100], responses[100:]

    assert all(response.status_code == 200 for response in accepted)
    assert all(response.status_code == 429 for response in exceeded)
    _assert_rate_headers(accepted[-1], limit=100, remaining=0)
    _assert_rate_headers(exceeded[0], limit=100, remaining=0)


def test_same_user_with_different_bearer_tokens_shares_quota_but_users_do_not() -> None:
    client = TestClient(_app())

    for token in ("token-one", "token-two"):
        response = client.get(
            "/api/v1/one",
            headers={"X-Test-User": "7", "Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    same_user = client.get("/api/v1/one", headers={"X-Test-User": "7"})
    other_user = client.get("/api/v1/one", headers={"X-Test-User": "8"})

    assert same_user.status_code == 429
    assert other_user.status_code == 200
    _assert_rate_headers(other_user, limit=2, remaining=1)


def test_extension_aliases_share_quota_while_tokens_are_isolated() -> None:
    client = TestClient(_app())
    first_token = {"X-Coleta-Token": "one"}

    first = client.post("/coleta", headers=first_token)
    alias = client.post("/api/v1/coleta", headers=first_token)
    exceeded = client.post("/coleta", headers=first_token)
    another_token = client.post("/api/v1/coleta", headers={"X-Coleta-Token": "two"})

    assert [first.status_code, alias.status_code, exceeded.status_code] == [200, 200, 429]
    assert another_token.status_code == 200
    _assert_rate_headers(exceeded, limit=2, remaining=0)
    _assert_rate_headers(another_token, limit=2, remaining=1)


def test_extension_token_spray_is_also_bounded_by_direct_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLETA_IP_RATE_LIMIT", "2/minute")
    get_settings.cache_clear()
    first_host = TestClient(_app(), client=("192.0.2.10", 50000))
    second_host = TestClient(_app(), client=("192.0.2.11", 50000))

    first = first_host.post("/coleta", headers={"X-Coleta-Token": "one"})
    second = first_host.post("/coleta", headers={"X-Coleta-Token": "two"})
    exceeded = first_host.post("/coleta", headers={"X-Coleta-Token": "three"})
    isolated = second_host.post("/coleta", headers={"X-Coleta-Token": "four"})

    assert [first.status_code, second.status_code, exceeded.status_code] == [200, 200, 429]
    assert exceeded.headers["X-RateLimit-Limit"] == "2"
    assert isolated.status_code == 200


def test_upload_post_uses_its_own_stricter_bucket_and_does_not_spend_api_quota() -> None:
    client = TestClient(_app())
    headers = {"X-Test-User": "7"}

    upload = client.post("/api/v1/upload", headers=headers)
    upload_exceeded = client.post("/api/v1/upload", headers=headers)
    api_after_upload = client.get("/api/v1/one", headers=headers)

    assert [upload.status_code, upload_exceeded.status_code, api_after_upload.status_code] == [
        200,
        429,
        200,
    ]
    _assert_rate_headers(upload, limit=1, remaining=0)
    _assert_rate_headers(upload_exceeded, limit=1, remaining=0)
    _assert_rate_headers(api_after_upload, limit=2, remaining=1)


def test_upload_default_window_returns_a_bounded_five_minute_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UPLOAD_RATE_LIMIT", "1/5minutes")
    get_settings.cache_clear()
    reset_rate_limiters()
    client = TestClient(_app())
    headers = {"X-Test-User": "7"}

    accepted = client.post("/api/v1/upload", headers=headers)
    exceeded = client.post("/api/v1/upload", headers=headers)

    retry_after = int(exceeded.headers["Retry-After"])
    assert accepted.status_code == 200
    assert exceeded.status_code == 429
    assert exceeded.json() == {"error": "rate_limited", "retry_after": retry_after}
    assert 1 <= retry_after <= 300
    assert time.time() < float(exceeded.headers["X-RateLimit-Reset"]) <= time.time() + 301


def test_auth_routes_share_a_bucket_per_direct_client_and_not_with_api() -> None:
    app = _app()
    first_host = TestClient(app, client=("192.0.2.10", 50000))
    second_host = TestClient(app, client=("192.0.2.11", 50000))

    assert first_host.post("/auth/login").status_code == 200
    assert first_host.post("/auth/refresh").status_code == 200
    assert first_host.post("/auth/login").status_code == 429
    assert second_host.post("/auth/login").status_code == 200
    assert first_host.get("/api/v1/one", headers={"X-Test-User": "7"}).status_code == 200


def test_reset_clears_every_policy_bucket() -> None:
    client = TestClient(_app())
    headers = {"X-Test-User": "7"}

    client.post("/api/v1/upload", headers=headers)
    assert client.post("/api/v1/upload", headers=headers).status_code == 429

    reset_rate_limiters()

    assert client.post("/api/v1/upload", headers=headers).status_code == 200


def test_reset_also_recovers_and_clears_the_in_memory_fallback() -> None:
    limiter = rate_limit.upload_limiter
    limiter._storage_dead = True
    client = TestClient(_app())
    headers = {"X-Test-User": "7"}

    assert client.post("/api/v1/upload", headers=headers).status_code == 200
    assert client.post("/api/v1/upload", headers=headers).status_code == 429

    reset_rate_limiters()

    assert limiter._storage_dead is False
    assert client.post("/api/v1/upload", headers=headers).status_code == 200


def test_unlimited_infrastructure_routes_have_no_rate_headers() -> None:
    client = TestClient(_app())

    responses = [client.get("/health") for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert all("X-RateLimit-Limit" not in response.headers for response in responses)


def test_production_middleware_authenticates_before_limiting_and_limits_before_body_read() -> None:
    layers = [layer.cls for layer in main.app.user_middleware]

    assert (
        layers.index(AuthRateLimitMiddleware)
        < layers.index(JWTAuthMiddleware)
        < layers.index(UserLogContextMiddleware)
        < layers.index(RateLimitMiddleware)
        < layers.index(RequestBodyLimitMiddleware)
    )


def test_production_body_limit_keeps_the_upload_envelope_margin() -> None:
    body_layer = next(
        layer for layer in main.app.user_middleware if layer.cls is RequestBodyLimitMiddleware
    )

    assert body_layer.kwargs["max_body_size"] == (
        get_settings().UPLOAD_MAX_BYTES + upload.MARGEM_DO_FORMULARIO
    )


def test_production_upload_stack_rate_limits_before_reading_an_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()

    def fake_db() -> Iterator[AsyncMock]:
        yield session

    def fake_user() -> SimpleNamespace:
        return SimpleNamespace(id=7)

    monkeypatch.setattr(auth_middleware, "verify_token", AsyncMock(return_value=7))
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", object)
    monkeypatch.setattr(upload, "_esperar_intervalo", AsyncMock(return_value=None))
    monkeypatch.setattr(upload, "_restante_do_dia", AsyncMock(return_value=None))
    main.app.dependency_overrides[get_db] = fake_db
    main.app.dependency_overrides[get_current_user] = fake_user
    client = TestClient(main.app, raise_server_exceptions=False)
    headers = {
        "Authorization": "Bearer valid-for-this-test",
        "Content-Length": str(get_settings().UPLOAD_MAX_BYTES + 1),
    }

    try:
        too_large = client.post("/api/v1/upload", headers=headers)
        exceeded = client.post("/api/v1/upload", headers=headers)
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        main.app.dependency_overrides.pop(get_current_user, None)

    assert too_large.status_code == 413
    assert exceeded.status_code == 429
    _assert_rate_headers(exceeded, limit=1, remaining=0)
    assert exceeded.json() == {
        "error": "rate_limited",
        "retry_after": int(exceeded.headers["Retry-After"]),
    }
