from types import SimpleNamespace

import httpx
import pytest

from bancaemdia.auth import transport

ORIGIN = "https://app.example.test"


@pytest.fixture
def identity_transport(monkeypatch):
    settings = SimpleNamespace(
        AUTH_ENABLED=True,
        AUTH_FRONTEND_ORIGIN=ORIGIN,
        AUTH_PUBLIC_URL=ORIGIN,
        session_cookie="session",
    )
    monkeypatch.setattr(transport, "identity_settings", lambda: settings)
    monkeypatch.setattr(
        transport,
        "identity_service",
        lambda: SimpleNamespace(keys=SimpleNamespace(csrf=lambda cookie: "proof:" + cookie)),
    )
    calls = []

    async def downstream(scope, receive, send):
        calls.append(scope["path"])
        await send({
            "type": "http.response.start",
            "status": 201,
            "headers": [(b"vary", b"Accept-Encoding")],
        })
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    return transport.IdentityTransportMiddleware(downstream), settings, calls


async def test_streamed_body_status_and_private_cors_headers_are_preserved(identity_transport):
    app, _, calls = identity_transport
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.get("/api/v1/apostas", headers={"origin": ORIGIN})
    assert calls == ["/api/v1/apostas"]
    assert response.status_code == 201 and response.content == b"firstsecond"
    assert response.headers["vary"] == "Accept-Encoding, Origin"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


@pytest.mark.parametrize(
    "path,invalid",
    [
        (path, invalid)
        for path in ["/api/v1/apostas", "/auth/refresh", "/auth/logout"]
        for invalid in ["origin", "proof", "cookie"]
        if invalid != "cookie" or path.startswith("/auth/")
    ],
)
async def test_cookie_writes_fail_before_downstream_on_each_csrf_violation(
    identity_transport, path, invalid
):
    app, _, calls = identity_transport
    headers = {"origin": ORIGIN, "cookie": "session=cookie", "x-csrf-token": "proof:cookie"}
    if invalid == "origin":
        headers["origin"] = "https://other.example.test"
    elif invalid == "proof":
        headers["x-csrf-token"] = "wrong"
    else:
        headers.pop("cookie")
        # Refresh/logout require a session; a bearer alone cannot bypass CSRF.
        headers["authorization"] = "Bearer present"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post(path, headers=headers)
    assert response.status_code == 403 and not calls
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("use_bearer", [False, True])
async def test_valid_cookie_proof_and_bearer_api_writes_reach_downstream(
    identity_transport, use_bearer
):
    app, _, calls = identity_transport
    headers = {"origin": ORIGIN, "cookie": "session=cookie"}
    headers.update(
        {"authorization": "Bearer present"} if use_bearer else {"x-csrf-token": "proof:cookie"}
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.post("/api/v1/apostas", headers=headers)
    assert response.status_code == 201 and len(calls) == 1


@pytest.mark.parametrize("violation", [None, "origin", "headers"])
async def test_preflight_preserves_exact_origin_and_header_allowlist(identity_transport, violation):
    app, _, calls = identity_transport
    headers = {"origin": ORIGIN, "access-control-request-headers": "Authorization, X-CSRF-Token"}
    if violation == "origin":
        headers["origin"] = "https://other.example.test"
    if violation == "headers":
        headers["access-control-request-headers"] = "X-Unapproved"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.options("/api/v1/apostas", headers=headers)
    assert response.status_code == (403 if violation else 204)
    assert not calls
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("path", ["/coleta", "/auth/session"])
async def test_disabled_identity_preserves_legacy_transport_and_auth_privacy(
    identity_transport, path
):
    app, settings, calls = identity_transport
    settings.AUTH_ENABLED = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.get(path, headers={"origin": "https://other.example.test"})
    assert response.status_code == 201 and response.content == b"firstsecond"
    assert len(calls) == 1
    assert "access-control-allow-origin" not in response.headers
    assert ("cache-control" in response.headers) == path.startswith("/auth/")
