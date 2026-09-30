"""Mandatory sandbox proof: Chromium -> real Keycloak/email -> actual API/PostgreSQL.

Run explicitly after tests/identity/docker-compose.yml is up. Network fault injection forwards
to the real issuer and can interrupt certs or all requests; no identity/token response is mocked.
"""

import asyncio
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.auth.identity_crypto import digest
from bancaemdia.auth.identity_service import IdentityService
from bancaemdia.config import get_settings
from tests.identity.support import key_settings

API = "http://127.0.0.1:58000"
FRONT = "http://127.0.0.1:58001"
ISSUER = "http://127.0.0.1:58080/realms/bancaemdia-acceptance"
MAIL = "http://127.0.0.1:58025"
CLIENT = "bancaemdia-acceptance"


@dataclass
class Network:
    fault: str = ""
    token_requests: int = 0


def serve_issuer(network):
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None  # Authorization codes, email-action links and passwords stay out of logs.

        def forward(self):
            path = urlsplit(self.path).path
            if network.fault == "issuer" or (network.fault == "jwks" and path.endswith("/certs")):
                self.send_response(503)
                self.end_headers()
                return
            if path.endswith("/token"):
                network.token_requests += 1
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in {"host", "connection", "accept-encoding"}
            }
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.request(
                    self.command,
                    "http://127.0.0.1:58081" + self.path,
                    content=body,
                    headers=headers,
                )
            self.send_response(response.status_code)
            for key, value in response.headers.multi_items():
                if key.lower() not in {
                    "transfer-encoding",
                    "content-length",
                    "connection",
                    "content-encoding",
                }:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = forward  # ruff: ignore[mixed-case-variable-in-class-scope] - BaseHTTPRequestHandler protocol
        do_POST = forward  # ruff: ignore[mixed-case-variable-in-class-scope] - BaseHTTPRequestHandler protocol

    return ThreadingHTTPServer(("127.0.0.1", 58080), Proxy)


def serve_frontend():
    class Frontend(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; connect-src "
                + API
                + "; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><title>Disposable identity acceptance</title><p>Session return destination</p>"
            )

    return ThreadingHTTPServer(("127.0.0.1", 58001), Frontend)


@dataclass
class Harness:
    service: IdentityService
    env: dict
    network: Network
    root: Path
    processes: list = field(default_factory=list)

    async def restart(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 15)
        # Raw uvicorn access logging is deliberately disabled for the sandbox subprocess too.
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "bancaemdia.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "58000",
                "--no-access-log",
            ],
            env=self.env,
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(
                        "Actual API subprocess did not start; check configuration and migration"
                    )
                try:
                    if (await client.get(API + "/health")).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        pytest.fail("Actual API did not become live")

    async def credentials(self, context):
        cookie = next(
            c["value"]
            for c in await context.cookies(API)
            if c["name"] == self.service.settings.session_cookie
        )
        async with self.service.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT id,encrypted FROM auth_private.sessions WHERE cookie_hash=:cookie"
                        ),
                        {"cookie": digest(cookie)},
                    )
                )
                .mappings()
                .one()
            )
        return cookie, json.loads(
            self.service.keys.open(row["encrypted"], "session:" + str(row["id"]))
        )


@pytest.fixture
async def harness(banco, engine_admin, tmp_path):
    assert os.environ.get("TEST_DATABASE_URL"), (
        "Identity acceptance requires explicit disposable PostgreSQL, never a skip"
    )
    network = Network()
    servers = [serve_issuer(network), serve_frontend()]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    async with httpx.AsyncClient(timeout=3) as client:
        for _ in range(180):
            try:
                if (
                    await client.get(ISSUER + "/.well-known/openid-configuration")
                ).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            pytest.fail("Real disposable issuer is not available")
    role, password = "identity_browser_" + uuid4().hex, secrets.token_urlsafe(32)
    async with engine_admin.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"))
        await conn.execute(text(f"GRANT bancaemdia_auth TO {role}"))
    url = (
        make_url(banco.url_admin)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )
    settings = key_settings(
        tmp_path,
        AUTH_ENABLED=True,
        AUTH_DATABASE_URL=url,
        AUTH_PUBLIC_URL=API,
        AUTH_FRONTEND_ORIGIN=FRONT,
        AUTH_COOKIE_SECURE=False,
        AUTH_ACCESS_SECONDS=30,
        OIDC_ISSUER=ISSUER,
        OIDC_CLIENT_ID=CLIENT,
    )
    engine = create_async_engine(url, hide_parameters=True, poolclass=NullPool)
    service = IdentityService(settings, engine)
    env = {
        **os.environ,
        **{
            key: str(value.get_secret_value() if hasattr(value, "get_secret_value") else value)
            for key, value in settings.model_dump().items()
            if value is not None
        },
    }
    env.update(
        DATABASE_URL=banco.url_app,
        DATABASE_URL_REPLICA=banco.url_app,
        AUTH_ENABLED="true",
        AUTH_COOKIE_SECURE="false",
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
        JWT_ISSUER=get_settings().JWT_ISSUER,
        JWT_AUDIENCE=get_settings().JWT_AUDIENCE,
        JWT_ALGORITHM="RS256",
        LOG_LEVEL="WARNING",
        OTEL_EXPORTER_OTLP_ENDPOINT="",
        RATE_LIMIT_STORAGE="memory://",
        AUTH_RATE_LIMIT="1000/minute",
        API_RATE_LIMIT="1000/minute",
    )
    harness = Harness(service, env, network, Path(__file__).resolve().parents[2])
    await harness.restart()
    try:
        yield harness
    finally:
        for process in harness.processes:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 15)
        for server in servers:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        await engine.dispose()
        async with engine_admin.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))


async def mail_link(email, seen=None):
    seen = seen or set()
    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(100):
            messages = (await client.get(MAIL + "/api/v1/messages")).json()["messages"]
            for message in messages:
                if message["ID"] in seen or not any(
                    address["Address"] == email for address in message["To"]
                ):
                    continue
                detail = (await client.get(MAIL + "/api/v1/message/" + message["ID"])).json()
                urls = re.findall(
                    r'https?://[^\s"<>]+',
                    html.unescape(detail.get("HTML", "") + " " + detail.get("Text", "")),
                )
                for url in urls:
                    if "/login-actions/action-token" in url:
                        return url, message["ID"]
            await asyncio.sleep(0.2)
    pytest.fail("The real issuer did not send the requested confirmation/recovery email")


async def fetch(page, path, *, method="GET", csrf=None, body=None):
    return await page.evaluate(
        """async ({api,path,method,csrf,body}) => {
      const headers = {}; if (csrf) headers['X-CSRF-Token']=csrf;
      if (body) headers['Content-Type']='application/json';
      const r=await fetch(api+path,{method,credentials:'include',headers,body:body?JSON.stringify(body):undefined});
      return {status:r.status,data:await r.json(),headers:Object.fromEntries(r.headers)};
    }""",
        {"api": API, "path": path, "method": method, "csrf": csrf, "body": body},
    )


async def mark_external_email_unconfirmed(email):
    # Control this disposable issuer to test a real signed, unconfirmed ID token on refresh.
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://127.0.0.1:58080/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": os.environ["KC_BOOTSTRAP_ADMIN_USERNAME"],
                "password": os.environ["KC_BOOTSTRAP_ADMIN_PASSWORD"],
            },
        )
        assert response.status_code == 200
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        base = "http://127.0.0.1:58080/admin/realms/bancaemdia-acceptance/users"
        users = (
            await client.get(base, params={"email": email, "exact": "true"}, headers=headers)
        ).json()
        assert len(users) == 1
        response = await client.put(
            base + "/" + users[0]["id"], headers=headers, json={"emailVerified": False}
        )
        assert response.status_code == 204


async def login(page, email, password, *, intent="login"):
    await page.goto(API + "/auth/start?return_to=/signed-in&intent=" + intent)
    if await page.locator("#username").is_visible():
        await page.locator("#username").fill(email)
        if not await page.locator("#password").count():
            await page.locator("#kc-login").click()
    try:
        await page.locator("#password").wait_for(timeout=5000)
    except BrowserTimeout:
        # No URLs with codes/action tokens, no input values, passwords or provider responses.
        controls = await page.locator("input").evaluate_all(
            "nodes=>nodes.map(n=>({id:n.id,name:n.name,type:n.type}))"
        )
        pytest.fail(
            "Hosted login has no password form: "
            + json.dumps({
                "path": urlsplit(page.url).path,
                "title": await page.title(),
                "controls": controls,
            })
        )
    await page.locator("#password").fill(password)
    await page.locator("#kc-login").click()
    await page.wait_for_url(FRONT + "/signed-in", timeout=30000)
    result = await fetch(page, "/auth/session")
    assert result["status"] == 200 and result["data"]["email"] == email
    return result["data"]


async def register(page, email, password, *, fail_jwks=False, harness=None):
    await page.goto(API + "/auth/start?return_to=/signed-in&intent=signup")
    await page.get_by_role("link", name="Register", exact=True).click()
    await page.locator("#email").fill(email)
    await page.locator("#firstName").fill("Disposable")
    await page.locator("#lastName").fill("Acceptance")
    # Keycloak 26.7 confirms email before asking the registrant to set a password.
    assert await page.locator("#password").count() == 0
    await page.locator('input[type="submit"],button[type="submit"]').click()
    link, message = await mail_link(email)
    # No API user can exist before email confirmation.
    async with harness.service.engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM usuarios WHERE email=:email"), {"email": email}
            )
            == 0
        )
    if fail_jwks:
        harness.network.fault = "jwks"
    await page.goto(link)
    await page.locator("#password-new").fill(password)
    await page.locator("#password-confirm").fill(password)
    await page.locator('input[type="submit"],button[type="submit"]').click()
    if fail_jwks:
        await page.wait_for_url(API + "/auth/callback?**", timeout=30000)
        assert (await page.locator("body").inner_text()).find("issuer_unavailable") >= 0
        async with harness.service.engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM usuarios WHERE email=:email"), {"email": email}
                )
                == 0
            )
        harness.network.fault = ""
        await asyncio.sleep(31)  # Existing JWKS anti-amplification cooldown, not a mocked clock.
        status = await login(page, email, password)
    else:
        await page.wait_for_url(FRONT + "/signed-in", timeout=30000)
        status = (await fetch(page, "/auth/session"))["data"]
    return status, message


async def test_real_registration_refresh_recovery_isolation_and_revocation(harness, engine_admin):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        a, b, recovery = [await browser.new_context() for _ in range(3)]
        pa, pb, pr = await a.new_page(), await b.new_page(), await recovery.new_page()
        email_a, email_b = (uuid4().hex + "@example.org" for _ in range(2))
        password_a, password_b, next_password = (secrets.token_urlsafe(24) for _ in range(3))
        sa, confirmation = await register(pa, email_a, password_a, fail_jwks=True, harness=harness)
        sb, _ = await register(pb, email_b, password_b, harness=harness)
        assert sa["usuario_id"] != sb["usuario_id"]
        assert sa["email"] == email_a and sb["email"] == email_b
        assert not {"access_token", "refresh_token", "id_token"}.intersection(sa)
        assert await pa.evaluate("Object.keys(localStorage).length") == 0
        cookies = await a.cookies(API)
        assert next(c for c in cookies if c["name"] == "bancaemdia_session")["httpOnly"] is True
        assert next(c for c in cookies if c["name"] == "bancaemdia_session")["sameSite"] == "Lax"

        assert (
            await fetch(
                pa,
                "/api/v1/apostas",
                method="POST",
                body={"casa": "betano", "odd": 2, "stake_unidades": 1},
            )
        )["status"] == 403
        sa = (await fetch(pa, "/auth/refresh", method="POST", csrf=sa["csrf_token"]))["data"]
        created = await fetch(
            pa,
            "/api/v1/apostas",
            method="POST",
            csrf=sa["csrf_token"],
            body={"casa": "betano", "odd": 2, "stake_unidades": 1},
        )
        assert created["status"] == 201
        key = created["data"]["aposta"]["chave"]
        assert (await fetch(pa, "/api/v1/apostas/" + key))["status"] == 200
        assert (await fetch(pb, "/api/v1/apostas/" + key))["status"] == 404
        assert (await fetch(pb, "/api/v1/apostas"))["data"]["pagination"]["total"] == 0
        cookie_before, credentials = await harness.credentials(a)
        async with httpx.AsyncClient() as client:
            invalid = await client.get(
                API + "/api/v1/apostas", headers={"Authorization": "Bearer invalid"}
            )
            assert invalid.status_code == 401
            foreign = await client.post(
                API + "/auth/logout",
                headers={
                    "Origin": "https://untrusted.example.org",
                    "X-CSRF-Token": sa["csrf_token"],
                },
                cookies={"bancaemdia_session": cookie_before},
            )
            assert (
                foreign.status_code == 403 and "access-control-allow-origin" not in foreign.headers
            )
            wrong_state = await client.get(
                API + "/auth/callback", params={"state": "forged", "code": "forged"}
            )
            assert wrong_state.status_code == 400
            empty_bearer = await client.post(
                API + "/api/v1/apostas",
                # A scheme without a credential is legal HTTP; trailing whitespace is rejected
                # by the HTTP client before it can exercise the API's CSRF/authentication gates.
                headers={"Authorization": "Bearer", "Origin": "https://untrusted.example.org"},
                cookies={"bancaemdia_session": cookie_before},
                json={"casa": "betano", "odd": 2, "stake_unidades": 1},
            )
            assert empty_bearer.status_code == 403

        # Real internal JWT expiry (no frozen clock), with session retained for renewal.
        import jwt

        exp = jwt.decode(credentials["internal"], options={"verify_signature": False})["exp"]
        await asyncio.sleep(max(0, exp - time.time()) + 1)
        assert (await fetch(pa, "/api/v1/apostas"))["status"] == 401
        assert (await fetch(pa, "/auth/session"))["data"]["refresh_required"] is True
        async with httpx.AsyncClient() as client:
            assert (
                await client.get(
                    API + "/api/v1/apostas",
                    headers={"Authorization": "Bearer " + credentials["internal"]},
                )
            ).status_code == 401

        # Provider rotates a real refresh token, then certs fail. Retry verifies the retained
        # encrypted response instead of trying an already consumed provider token.
        await harness.restart()
        harness.network.fault = "jwks"
        failed = await fetch(pa, "/auth/refresh", method="POST", csrf=sa["csrf_token"])
        assert failed["status"] == 503
        grants = harness.network.token_requests
        harness.network.fault = ""
        await asyncio.sleep(31)
        renewed = await fetch(pa, "/auth/refresh", method="POST", csrf=sa["csrf_token"])
        assert renewed["status"] == 200
        assert harness.network.token_requests == grants
        sa = renewed["data"]
        cookie_after, live = await harness.credentials(a)
        assert cookie_after != cookie_before and sa["session_version"].endswith(":2")
        assert (await fetch(pa, "/api/v1/apostas"))["status"] == 200

        harness.network.fault = "issuer"
        assert (await fetch(pa, "/auth/refresh", method="POST", csrf=sa["csrf_token"]))[
            "status"
        ] == 503
        assert (await fetch(pa, "/auth/session"))["status"] == 200
        harness.network.fault = ""
        assert (await fetch(pa, "/auth/refresh", method="POST", csrf=sa["csrf_token"]))[
            "status"
        ] == 200
        async with httpx.AsyncClient() as client:
            # Still within exp, but the JWT generation changed with the successful refresh.
            assert (
                await client.get(
                    API + "/api/v1/apostas", headers={"Authorization": "Bearer " + live["internal"]}
                )
            ).status_code == 401

        # Real hosted password recovery and SMTP mail, preserving the external/internal link.
        await pr.goto(API + "/auth/start?intent=recover&return_to=/signed-in")
        await pr.get_by_role("link", name="Forgot Password?", exact=True).click()
        await pr.locator("#username").fill(email_a)
        await pr.locator('input[type="submit"],button[type="submit"]').click()
        link, _ = await mail_link(email_a, {confirmation})
        await pr.goto(link)
        await pr.locator("#password-new").fill(next_password)
        await pr.locator("#password-confirm").fill(next_password)
        await pr.locator('input[type="submit"],button[type="submit"]').click()
        await pr.wait_for_url(FRONT + "/signed-in", timeout=30000)
        sr = (await fetch(pr, "/auth/session"))["data"]
        assert sr["usuario_id"] == sa["usuario_id"]
        assert (await fetch(pa, "/api/v1/apostas"))["status"] == 401
        cookie_recovery, live = await harness.credentials(recovery)
        await pa.goto(API + "/auth/start?return_to=/signed-in")
        if await pa.locator("#username").is_visible():
            await pa.locator("#username").fill(email_a)
        await pa.locator("#password").fill(password_a)
        await pa.locator("#kc-login").click()
        await pa.get_by_text("Invalid username or password.", exact=True).wait_for()
        async with httpx.AsyncClient() as client:
            assert (
                await client.get(
                    API + "/api/v1/apostas", headers={"Authorization": "Bearer " + live["internal"]}
                )
            ).status_code == 200

        # Logout commits local revocation while the real issuer is down; delivery is retried.
        harness.network.fault = "issuer"
        logout = await fetch(
            pr, "/auth/logout?all_sessions=true", method="POST", csrf=sr["csrf_token"]
        )
        assert logout["status"] == 200 and logout["data"] == {"logged_out": True}
        async with httpx.AsyncClient() as client:
            assert (
                await client.get(
                    API + "/api/v1/apostas", headers={"Authorization": "Bearer " + live["internal"]}
                )
            ).status_code == 401
            assert (
                await client.get(
                    API + "/auth/session", cookies={"bancaemdia_session": cookie_recovery}
                )
            ).status_code == 401
        harness.network.fault = ""
        async with engine_admin.begin() as conn:
            await conn.execute(text("UPDATE auth_private.revocations SET retry_at=now()"))
        await harness.service.drain_revocations()
        async with httpx.AsyncClient() as client:
            rejected = await client.post(
                ISSUER + "/protocol/openid-connect/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT,
                    "refresh_token": live["refresh"],
                },
            )
            assert rejected.status_code == 400

        # New credentials work; a retired browser cookie revokes the refreshed family.
        sa = await login(pa, email_a, next_password)
        stale, _ = await harness.credentials(a)
        refreshed = await fetch(pa, "/auth/refresh", method="POST", csrf=sa["csrf_token"])
        assert refreshed["status"] == 200
        async with httpx.AsyncClient() as client:
            reused = await client.post(
                API + "/auth/refresh",
                headers={"Origin": FRONT, "X-CSRF-Token": sa["csrf_token"]},
                cookies={"bancaemdia_session": stale},
            )
            assert reused.status_code == 401 and reused.json()["code"] == "refresh_reused"
        assert (await fetch(pa, "/auth/session"))["status"] == 401
        await mark_external_email_unconfirmed(email_b)
        refused = await fetch(pb, "/auth/refresh", method="POST", csrf=sb["csrf_token"])
        assert refused["status"] == 403 and refused["data"]["code"] == "email_unconfirmed"
        assert (await fetch(pb, "/auth/session"))["status"] == 401
        sa = await login(pa, email_a, next_password)
        race_cookie, _ = await harness.credentials(a)
        grants = harness.network.token_requests
        async with httpx.AsyncClient() as client:
            replies = await asyncio.gather(
                *(
                    client.post(
                        API + "/auth/refresh",
                        headers={
                            "Origin": FRONT,
                            "X-CSRF-Token": sa["csrf_token"],
                            "Cookie": "bancaemdia_session=" + race_cookie,
                        },
                    )
                    for _ in range(3)
                )
            )
        assert sum(reply.status_code == 200 for reply in replies) <= 1
        assert all(reply.status_code in {200, 401} for reply in replies)
        assert any(
            reply.status_code == 401 and reply.json()["code"] == "refresh_reused"
            for reply in replies
        )
        assert harness.network.token_requests == grants + 1
        assert (await fetch(pa, "/auth/session"))["status"] == 401
        sa = await login(pa, email_a, next_password)
        async with engine_admin.begin() as conn:
            await conn.execute(
                text("UPDATE usuarios SET ativo=false WHERE id=:id"), {"id": sa["usuario_id"]}
            )
        assert (await fetch(pa, "/auth/session"))["status"] == 401
        assert (await fetch(pa, "/api/v1/apostas"))["status"] == 401
        await browser.close()
