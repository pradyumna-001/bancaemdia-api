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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import ViewportSize, async_playwright
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.auth.identity_crypto import digest
from bancaemdia.auth.identity_service import IdentityService
from bancaemdia.config import get_settings
from tests.identity import diagnostics, ports
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


def reserved_server(port: int, handler: Any) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler, bind_and_activate=False)
    server.socket.close()
    # server_close() closes only this duplicate; see tests/identity/ports.py.
    server.socket = ports.listener(port)
    server.server_address = server.socket.getsockname()
    server.server_name, server.server_port = "127.0.0.1", port
    return server


def serve_issuer(network: Any) -> Any:
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> Any:
            return None  # Authorization codes, email-action links and passwords stay out of logs.

        def forward(self) -> Any:
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
            try:
                with httpx.Client(timeout=15, follow_redirects=False) as client:
                    response = client.request(
                        self.command,
                        "http://127.0.0.1:58081" + self.path,
                        content=body,
                        headers=headers,
                    )
            except httpx.HTTPError:
                self.send_response(503)
                self.end_headers()
                return
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
            try:
                self.wfile.write(response.content)
            except (BrokenPipeError, ConnectionResetError):
                return  # A browser navigation can cancel an earlier asset/protocol response.

        do_GET = forward  # ruff: ignore[mixed-case-variable-in-class-scope] - BaseHTTPRequestHandler protocol
        do_POST = forward  # ruff: ignore[mixed-case-variable-in-class-scope] - BaseHTTPRequestHandler protocol
        do_PUT = forward  # ruff: ignore[mixed-case-variable-in-class-scope] - disposable issuer admin protocol

    return reserved_server(58080, Proxy)


def serve_frontend() -> Any:
    class Frontend(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> Any:
            return None

        def do_GET(self) -> Any:
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

    return reserved_server(58001, Frontend)


async def stop_servers(servers: list[Any]) -> None:
    for server in servers:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


async def wait_for_issuer(journal: diagnostics.Journal) -> None:
    """The realm answers discovery, publishes signing keys and renders the hosted login.

    Keycloak serves discovery as soon as HTTP is up; the first hosted page can still
    fail while themes and the imported realm finish warming. A journey starts only after
    the same three things a browser needs are ready.
    """
    started, last = time.monotonic(), ""
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() - started < 180:
            last = ""
            try:
                discovery = await client.get(ISSUER + "/.well-known/openid-configuration")
                if discovery.status_code != 200:
                    last = f"discovery {discovery.status_code}"
                    raise LookupError
                metadata = discovery.json()
                certs = await client.get(metadata["jwks_uri"])
                if certs.status_code != 200 or not certs.json().get("keys"):
                    last = f"jwks {certs.status_code}"
                    raise LookupError
                hosted = await client.get(
                    metadata["authorization_endpoint"],
                    params={
                        "client_id": CLIENT,
                        "response_type": "code",
                        "scope": "openid",
                        "redirect_uri": API + "/auth/callback",
                        # The client requires PKCE; this throwaway request is never completed.
                        "state": secrets.token_urlsafe(16),
                        "code_challenge": secrets.token_urlsafe(32),
                        "code_challenge_method": "S256",
                    },
                )
                if hosted.status_code != 200 or 'id="kc-form-login"' not in hosted.text:
                    last = f"hosted login {hosted.status_code}"
                    raise LookupError
                journal.record("issuer.ready", f"after={time.monotonic() - started:.1f}s")
                return
            except (LookupError, KeyError, ValueError, httpx.HTTPError) as error:
                last = last or type(error).__name__
            await asyncio.sleep(1)
    journal.record("issuer.unavailable", last)
    pytest.fail(f"Real disposable issuer is not ready after 180 s (last: {last})", pytrace=False)


@dataclass
class Harness:
    service: IdentityService
    env: dict[str, str]
    network: Network
    root: Path
    processes: list[Any] = field(default_factory=list)

    @property
    def journal(self) -> diagnostics.Journal:
        directory = os.environ.get("IDENTITY_DIAGNOSTICS_DIR") or self.root / "identity-diagnostics"
        return diagnostics.Journal(Path(directory), self.env)

    async def stop(self) -> None:
        for process in self.processes:
            if process.poll() is not None:
                continue
            started = time.monotonic()
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 15)
            except subprocess.TimeoutExpired:
                # A graceful shutdown can wait forever on a kept-alive browser connection.
                self.journal.record("process.kill", f"pid={process.pid} ignored SIGTERM for 15s")
                process.kill()
                await asyncio.to_thread(process.wait, 15)
            self.journal.record(
                "process.stop",
                f"pid={process.pid} exit={process.returncode}"
                f" after={time.monotonic() - started:.1f}s",
            )

    async def restart(self) -> Any:
        # Every previous API (and any owned helper process) is gone before the next starts:
        # two APIs on one socket would serve requests with stale configuration.
        await self.stop()
        journal = self.journal
        listener = ports.listener(58000, journal)
        listener.set_inheritable(True)
        stderr_path = journal.directory / f"api-{uuid4().hex[:12]}.stderr"
        with stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("api_server.py")),
                    str(listener.fileno()),
                ],
                env=self.env,
                cwd=self.root,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                pass_fds=(listener.fileno(),),
            )
        listener.close()  # The child holds its own copy; the reserved original stays here.
        self.processes.append(process)
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=2) as client:
            while time.monotonic() - started < 120:
                if process.poll() is not None:
                    journal.record("api.exit", f"pid={process.pid} code={process.returncode}")
                    pytest.fail(
                        "Actual API subprocess did not start; check configuration and migration"
                        f" (exit code {process.returncode}; sanitized stderr {stderr_path.name}):\n"
                        + diagnostics.tail(stderr_path, self.env),
                        pytrace=False,
                    )
                try:
                    if (await client.get(API + "/health")).status_code == 200:
                        journal.record(
                            "api.live",
                            f"pid={process.pid} after={time.monotonic() - started:.1f}s",
                        )
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        journal.record("api.not_live", f"pid={process.pid}")
        pytest.fail(
            "Actual API did not become live in 120 s; last sanitized stderr:\n"
            + diagnostics.tail(stderr_path, self.env),
            pytrace=False,
        )

    async def credentials(self, context: Any) -> Any:
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
async def harness(banco: Any, engine_admin: Any, tmp_path: Any) -> AsyncIterator[Any]:
    assert os.environ.get("TEST_DATABASE_URL"), (
        "Identity acceptance requires explicit disposable PostgreSQL, never a skip"
    )
    root = Path(__file__).resolve().parents[2]
    journal = diagnostics.Journal(
        Path(os.environ.get("IDENTITY_DIAGNOSTICS_DIR") or root / "identity-diagnostics"),
        os.environ,
    )
    # Reserve before this harness starts its own servers or API (see ports.py).
    await ports.reserve(58000, journal)
    await ports.reserve(58080, journal)
    network = Network()
    servers = [serve_issuer(network), serve_frontend()]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        await wait_for_issuer(journal)
    except BaseException:
        await stop_servers(servers)
        raise
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
    harness = Harness(service, env, network, root)
    try:
        # Inside try: a failed first start must still free the port, servers and role.
        await harness.restart()
        yield harness
    finally:
        await harness.stop()
        await stop_servers(servers)
        await engine.dispose()
        async with engine_admin.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))


async def mail_link(email: Any, seen: Any = None) -> Any:
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


async def fetch(
    page: Any, path: Any, *, method: Any = "GET", csrf: Any = None, body: Any = None
) -> Any:
    return await page.evaluate(
        """async ({api,path,method,csrf,body}) => {
      const headers = {}; if (csrf) headers['X-CSRF-Token']=csrf;
      if (body) headers['Content-Type']='application/json';
      const r=await fetch(api+path,{method,credentials:'include',headers,body:body?JSON.stringify(body):undefined});
      return {status:r.status,data:await r.json(),headers:Object.fromEntries(r.headers)};
    }""",
        {"api": API, "path": path, "method": method, "csrf": csrf, "body": body},
    )


async def mark_external_email_unconfirmed(email: Any) -> Any:
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


async def login(page: Any, email: Any, password: Any, *, intent: Any = "login") -> Any:
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
    try:
        await page.wait_for_url(FRONT + "/signed-in", timeout=30000)
    except BrowserTimeout:
        state = await diagnostics.hosted_state(page)
        issuer = await diagnostics.issuer_user(email)
        pytest.fail(
            "Hosted login did not reach /signed-in: " + json.dumps({**state, "issuer": issuer}),
            pytrace=False,
        )
    result = await fetch(page, "/auth/session")
    assert result["status"] == 200 and result["data"]["email"] == email
    return result["data"]


async def require_complete_account(email: str, journal: diagnostics.Journal) -> None:
    # A later hosted login would stop on a pending required action with no clue why.
    issuer = await diagnostics.issuer_user(email)
    journal.record("register.issuer", json.dumps(issuer))
    if "requiredActions" in issuer and (
        issuer["requiredActions"]
        or issuer["emailVerified"] is not True
        or "password" not in issuer["credentials"]
    ):
        pytest.fail("Issuer left the new account incomplete: " + json.dumps(issuer), pytrace=False)


async def submit_registration(page: Any, journal: diagnostics.Journal) -> None:
    # Like a person, open the email only after the issuer answered the form. Following
    # the action link while the registration request is still in flight lets the two
    # requests race on the same Keycloak authentication session.
    try:
        async with page.expect_navigation(wait_until="load", timeout=30000):
            await page.locator('input[type="submit"],button[type="submit"]').click()
    except BrowserTimeout:
        state = await diagnostics.hosted_state(page)
        journal.record("register.submit.timeout", json.dumps(state))
        pytest.fail("Hosted registration form got no answer: " + json.dumps(state), pytrace=False)


async def register(
    page: Any, email: Any, password: Any, *, fail_jwks: Any = False, harness: Any = None
) -> Any:
    await page.goto(API + "/auth/start?return_to=/signed-in&intent=signup")
    await page.get_by_role("link", name="Register", exact=True).click()
    await page.locator("#email").fill(email)
    await page.locator("#firstName").fill("Disposable")
    await page.locator("#lastName").fill("Acceptance")
    # Keycloak 26.7 confirms email before asking the registrant to set a password.
    assert await page.locator("#password").count() == 0
    await submit_registration(page, harness.journal)
    link, message = await mail_link(email)
    journal = harness.journal
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
        try:
            await page.wait_for_url(FRONT + "/signed-in", timeout=30000)
        except BrowserTimeout:
            state = await diagnostics.hosted_state(page)
            issuer = await diagnostics.issuer_user(email)
            journal.record("register.timeout", json.dumps({**state, "issuer": issuer}))
            pytest.fail(
                "Hosted registration did not reach /signed-in: "
                + json.dumps({**state, "issuer": issuer}),
                pytrace=False,
            )
        status = (await fetch(page, "/auth/session"))["data"]
    await require_complete_account(email, journal)
    return status, message


async def test_real_registration_refresh_recovery_isolation_and_revocation(
    harness: Any, engine_admin: Any
) -> None:
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
        # Exercise these new GETs through the real hosted-login HttpOnly session.
        for dimensao in [
            "casas",
            "tipsters",
            "mercados",
            "competicoes",
            "origens",
            "grupos",
            "bancas",
            "titulares",
            "contas",
        ]:
            url = API + "/api/v1/filtros/" + dimensao
            async with pa.expect_response(url) as received:
                opcoes = await fetch(pa, "/api/v1/filtros/" + dimensao)
            assert opcoes["status"] == 200
            assert opcoes["headers"]["cache-control"] == "private, no-store"
            # Vary controls HTTP caches; it is not a CORS-exposed JS header.
            # Inspect the actual browser network response without weakening CORS.
            wire_headers = await (await received.value).all_headers()
            assert {"Authorization", "Cookie", "Origin"} <= {
                item.strip() for item in wire_headers["vary"].split(",")
            }
        grupo = await fetch(
            pa,
            "/api/v1/grupos",
            method="POST",
            csrf=sa["csrf_token"],
            body={"nome": "Grupo privado descartável"},
        )
        assert grupo["status"] == 201
        gid = grupo["data"]["id"]
        assert isinstance(gid, str)
        assert (await fetch(pb, "/api/v1/filtros/grupos?id=" + gid))["data"]["data"] == []
        atribuir = await fetch(
            pa,
            "/api/v1/apostas/" + key + "/grupos",
            method="PUT",
            csrf=sa["csrf_token"],
            body={"grupo_ids": [gid]},
        )
        assert atribuir["status"] == 200
        query = "?grupo_id=" + gid + "&visibilidade=ativas"
        resumo = await fetch(pa, "/api/v1/painel/filtrado" + query)
        assert resumo["status"] == 200 and resumo["data"]["resumo"]["total_apostas"] == 1
        assert (await fetch(pb, "/api/v1/painel/filtrado" + query))["data"]["resumo"][
            "total_apostas"
        ] == 0
        graficos = await fetch(pa, "/api/v1/painel/filtrado/metricas" + query)
        assert graficos["status"] == 200 and graficos["data"]["total_periodo"]["total_apostas"] == 1
        arquivo = await pa.evaluate(
            """async url => {const r=await fetch(url,{credentials:'include'});return {status:r.status,type:r.headers.get('content-type'),bytes:(await r.arrayBuffer()).byteLength}}""",
            API + "/api/v1/painel/filtrado/export" + query,
        )
        assert arquivo["status"] == 200 and arquivo["bytes"] > 1000
        assert "spreadsheetml" in arquivo["type"]
        # New comparison uses the same genuine hosted-login HttpOnly session.
        for viewport in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
            await pa.set_viewport_size(cast(ViewportSize, viewport))
            own = await fetch(pa, "/api/v1/painel/hipotetica")
            other = await fetch(pb, "/api/v1/painel/hipotetica")
            assert own["status"] == other["status"] == 200
            assert own["data"]["apostas_consideradas"] == 1
            assert other["data"]["apostas_consideradas"] == 0
            assert own["data"]["real"] == own["data"]["hipotetico"]
            assert own["headers"]["cache-control"] == "private, no-store"
        await pa.set_viewport_size({"width": 1280, "height": 720})
        # Same genuine HttpOnly session on desktop and mobile viewports.
        for viewport in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
            await pa.set_viewport_size(cast(ViewportSize, viewport))
            for path in (
                "/api/v1/painel/filtrado/grupos",
                "/api/v1/painel/filtrado/diario?periodo=1y",
            ):
                own = await fetch(pa, path)
                other = await fetch(pb, path)
                assert own["status"] == other["status"] == 200
                assert own["data"]["total_periodo"]["total_apostas"] == 1
                assert other["data"]["total_periodo"]["total_apostas"] == 0
        await pa.set_viewport_size({"width": 1280, "height": 720})
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
        async with engine_admin.connect() as conn:
            revoked = (
                (
                    await conn.execute(
                        text(
                            "SELECT s.id,s.encrypted,r.session_id FROM auth_private.sessions s "
                            "JOIN auth_private.revocations r ON r.session_id=s.id "
                            "WHERE s.revoked_at IS NOT NULL"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert revoked
            for row in revoked:
                assert (
                    json.loads(
                        harness.service.keys.open(row["encrypted"], "session:" + str(row["id"]))
                    )
                    == {}
                )
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
        async with engine_admin.connect() as conn:
            assert (
                await conn.execute(text("SELECT count(*) FROM auth_private.revocations"))
            ).scalar_one() == 0
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
