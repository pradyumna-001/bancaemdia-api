"""The issuer owns credentials; this service owns internal links and session capability."""

import json
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bancaemdia.auth.identity_config import IdentitySettings, identity_settings
from bancaemdia.auth.identity_crypto import IdentityKeys, digest
from bancaemdia.auth.jwt import InvalidTokenError, verified_claims, verify_token
from bancaemdia.auth.oidc import IdentityError, OIDCClient
from bancaemdia.config import get_settings


def destination(value: str) -> str:
    decoded = unquote(value)
    if (
        not value.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(char) < 32 for char in decoded)
        or len(value) > 2048
        or "#" in decoded
        or decoded.startswith("/auth/")
    ):
        raise IdentityError("invalid_destination", 400)
    return value


def now() -> datetime:
    return datetime.now(UTC)


class IdentityService:
    def __init__(self, settings: IdentitySettings, engine: AsyncEngine) -> None:
        self.settings = settings
        self.engine = engine
        if get_settings().JWT_ALGORITHM != "RS256":
            raise ValueError("Identity sessions require the existing RS256 JWT validator")
        self.keys = IdentityKeys(settings)
        self.oidc = OIDCClient(settings)

    async def begin(self, return_to: str, intent: str = "login") -> tuple[str, str]:
        return_to = destination(return_to)
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        url = await self.oidc.authorization(state, nonce, verifier)
        state_hash = digest(state)
        payload = self.keys.seal(
            json.dumps({
                "nonce": nonce,
                "verifier": verifier,
                "return_to": return_to,
                "intent": intent,
            }),
            "flow:" + state_hash,
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO auth_private.flows(state_hash,browser_hash,encrypted,expires_at) VALUES(:state,:browser,:encrypted,:expires)"
                ),
                {
                    "state": state_hash,
                    "browser": digest(browser),
                    "encrypted": payload,
                    "expires": now() + timedelta(minutes=10),
                },
            )
        return url, browser

    async def callback(self, state: str, browser: str, code: str) -> tuple[str, str]:
        if not state or not browser or not code or max(len(state), len(browser), len(code)) > 4096:
            raise IdentityError("invalid_flow", 400)
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE auth_private.flows SET consumed_at=now() WHERE state_hash=:state AND browser_hash=:browser AND consumed_at IS NULL AND expires_at>now() RETURNING encrypted"
                    ),
                    {"state": digest(state), "browser": digest(browser)},
                )
            ).scalar_one_or_none()
        if row is None:
            raise IdentityError("invalid_flow", 400)
        flow = json.loads(self.keys.open(row, "flow:" + digest(state)))
        # A claimed flow is never re-used, even if the external exchange/JWKS fails.
        # A temporary 503 asks the browser to start a new login, without discarding other sessions.
        tokens = await self.oidc.tokens({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.callback,
            "code_verifier": flow["verifier"],
        })
        claims = await self.oidc.identity(tokens["id_token"], flow["nonce"])
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise IdentityError("refresh_not_available", 503)
        cookie = secrets.token_urlsafe(32)
        async with AsyncSession(self.engine) as session, session.begin():
            user, identity = await self.provision(session, claims)
            if flow["intent"] == "recover":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:identity,168))"),
                    {"identity": identity},
                )
                previous = list(
                    (
                        await session.execute(
                            text(
                                "SELECT id FROM auth_private.sessions WHERE identity_id=:identity ORDER BY id FOR UPDATE"
                            ),
                            {"identity": identity},
                        )
                    ).scalars()
                )
                for previous_id in previous:
                    await self._revoke(session, str(previous_id), "logout")
            sid = str(uuid4())
            encrypted = self._credentials(sid, user, 0, tokens)
            await session.execute(
                text(
                    "INSERT INTO auth_private.sessions(id,identity_id,cookie_hash,encrypted,expires_at,idle_until,access_until) VALUES(:id,:identity,:cookie,:encrypted,:expires,:idle,:access)"
                ),
                {
                    "id": sid,
                    "identity": identity,
                    "cookie": digest(cookie),
                    "encrypted": encrypted,
                    "expires": now() + timedelta(seconds=self.settings.AUTH_SESSION_SECONDS),
                    "idle": now() + timedelta(seconds=self.settings.AUTH_IDLE_SECONDS),
                    "access": now() + timedelta(seconds=self.settings.AUTH_ACCESS_SECONDS),
                },
            )
            await self._audit(session, user, sid, "login")
        return cookie, self.settings.AUTH_FRONTEND_ORIGIN.rstrip("/") + destination(
            flow["return_to"]
        )

    async def provision(self, session: AsyncSession, claims: dict[str, Any]) -> tuple[int, str]:
        if (
            claims.get("iss") != self.settings.OIDC_ISSUER
            or claims.get("email_verified") is not True
        ):
            raise IdentityError("identity_rejected")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:binding,167))"),
            {"binding": json.dumps([claims["iss"], claims["sub"]])},
        )
        row = (
            (
                await session.execute(
                    text(
                        "SELECT i.id,u.id AS usuario_id,u.ativo FROM auth_private.identities i JOIN public.usuarios u ON u.id=i.usuario_id WHERE i.issuer=:issuer AND i.subject=:subject"
                    ),
                    {"issuer": claims["iss"], "subject": claims["sub"]},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            if not row["ativo"]:
                raise IdentityError("account_inactive")
            return int(row["usuario_id"]), str(row["id"])
        email = claims["email"].strip().casefold()
        try:
            async with session.begin_nested():
                user = await session.scalar(
                    text(
                        "INSERT INTO public.usuarios(email,nome) VALUES(:email,:name) RETURNING id"
                    ),
                    {"email": email, "name": str(claims.get("name") or email)[:200]},
                )
                identity = str(uuid4())
                await session.execute(
                    text(
                        "INSERT INTO auth_private.identities(id,issuer,subject,usuario_id) VALUES(:id,:issuer,:subject,:user)"
                    ),
                    {
                        "id": identity,
                        "issuer": claims["iss"],
                        "subject": claims["sub"],
                        "user": user,
                    },
                )
        except IntegrityError as error:
            # Even a verified email coincidence never proves ownership of a pre-existing account.
            raise IdentityError("identity_conflict", 409) from error
        assert user is not None
        await self._audit(session, int(user), None, "provision")
        return int(user), identity

    def _credentials(self, sid: str, user: int, generation: int, tokens: dict[str, Any]) -> str:
        internal = self.keys.issue(user, sid, self.settings.AUTH_ACCESS_SECONDS, generation)
        return self.keys.seal(
            json.dumps({
                "internal": internal,
                "generation": generation,
                "refresh": tokens["refresh_token"],
                "id_token": tokens["id_token"],
            }),
            "session:" + sid,
        )

    async def _lookup(
        self, session: AsyncSession, cookie: str, *, lock: bool = False
    ) -> dict[str, Any]:
        if not cookie or len(cookie) > 256:
            raise IdentityError("not_authenticated")
        sql = "SELECT s.*,i.issuer,i.subject,i.usuario_id,u.ativo,u.nome,u.email FROM auth_private.sessions s JOIN auth_private.identities i ON i.id=s.identity_id JOIN public.usuarios u ON u.id=i.usuario_id WHERE s.cookie_hash=:cookie"
        row = (
            (await session.execute(text(sql), {"cookie": digest(cookie)})).mappings().one_or_none()
        )
        if lock and row is not None:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:identity,168))"),
                {"identity": str(row["identity_id"])},
            )
            row = (
                (await session.execute(text(sql + " FOR UPDATE OF s"), {"cookie": digest(cookie)}))
                .mappings()
                .one_or_none()
            )
        if (
            row is None
            or row["revoked_at"] is not None
            or min(row["expires_at"], row["idle_until"]) <= now()
        ):
            raise IdentityError("session_expired")
        if not row["ativo"]:
            raise IdentityError("account_inactive")
        return dict(row)

    async def authenticate(self, cookie: str) -> int:
        async with AsyncSession(self.engine) as session:
            row = await self._lookup(session, cookie)
        if row["access_until"] <= now():
            raise IdentityError("access_expired")
        credentials = json.loads(self.keys.open(row["encrypted"], "session:" + str(row["id"])))
        try:
            user = await verify_token(credentials["internal"], self.keys.cache)
        except InvalidTokenError as error:
            raise IdentityError("access_expired") from error
        if user != row["usuario_id"]:
            raise IdentityError("identity_rejected")
        return user

    async def authenticate_bearer(self, token: str) -> int:
        try:
            claims = await verified_claims(token, self.keys.cache)
            sid = claims.get("sid")
            if not isinstance(sid, str):
                raise IdentityError("identity_rejected")
            async with AsyncSession(self.engine) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT s.generation,s.revoked_at,s.expires_at,s.idle_until,i.usuario_id,u.ativo FROM auth_private.sessions s JOIN auth_private.identities i ON i.id=s.identity_id JOIN public.usuarios u ON u.id=i.usuario_id WHERE s.id::text=:id"
                            ),
                            {"id": sid},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if (
                row is None
                or row["revoked_at"] is not None
                or not row["ativo"]
                or min(row["expires_at"], row["idle_until"]) <= now()
                or claims.get("ver") != row["generation"]
                or int(claims["sub"]) != row["usuario_id"]
            ):
                raise IdentityError("session_expired")
            return int(claims["sub"])
        except InvalidTokenError as error:
            raise IdentityError("identity_rejected") from error

    async def status(self, cookie: str) -> dict[str, Any]:
        async with AsyncSession(self.engine) as session:
            row = await self._lookup(session, cookie)
        return {
            "usuario_id": row["usuario_id"],
            "nome": row["nome"],
            "email": row["email"],
            "session_version": str(row["id"]) + ":" + str(row["generation"]),
            "csrf_token": self.keys.csrf(cookie),
            "access_expires_at": row["access_until"],
            "session_expires_at": min(row["expires_at"], row["idle_until"]),
            "refresh_required": row["access_until"] <= now(),
        }

    async def refresh(self, cookie: str) -> str:
        new_cookie = secrets.token_urlsafe(32)
        failure: IdentityError | None = None
        async with AsyncSession(self.engine) as session, session.begin():
            retired = await session.scalar(
                text(
                    "SELECT session_id FROM auth_private.retired_cookies WHERE cookie_hash=:cookie"
                ),
                {"cookie": digest(cookie)},
            )
            if retired is not None:
                await self._revoke(session, str(retired), "reuse")
                failure = IdentityError("refresh_reused")
            else:
                row = await self._lookup(session, cookie, lock=True)
                sid = str(row["id"])
                credentials = json.loads(self.keys.open(row["encrypted"], "session:" + sid))
                tokens: dict[str, Any] | None = credentials.get("pending")
                try:
                    if tokens is None:
                        tokens = await self.oidc.tokens({
                            "grant_type": "refresh_token",
                            "refresh_token": credentials["refresh"],
                        })
                    claims = await self.oidc.identity(tokens["id_token"], None)
                    if claims["iss"] != row["issuer"] or claims["sub"] != row["subject"]:
                        raise IdentityError("identity_rejected")
                except IdentityError as error:
                    if error.status == 503:
                        # A successful grant may already have rotated the provider token. Retain
                        # the encrypted response without authorizing it; retry verification only.
                        if tokens is not None:
                            credentials["pending"] = tokens
                            credentials["refresh"] = (
                                tokens.get("refresh_token") or credentials["refresh"]
                            )
                            await session.execute(
                                text(
                                    "UPDATE auth_private.sessions SET encrypted=:encrypted WHERE id=:id"
                                ),
                                {
                                    "id": sid,
                                    "encrypted": self.keys.seal(
                                        json.dumps(credentials), "session:" + sid
                                    ),
                                },
                            )
                    else:
                        await self._revoke(session, sid, "logout")
                    failure = error
                if failure is None:
                    assert tokens is not None
                    tokens["refresh_token"] = tokens.get("refresh_token") or credentials["refresh"]
                    encrypted = self._credentials(
                        sid, row["usuario_id"], row["generation"] + 1, tokens
                    )
                    await session.execute(
                        text(
                            "INSERT INTO auth_private.retired_cookies(cookie_hash,session_id) VALUES(:cookie,:id)"
                        ),
                        {"cookie": digest(cookie), "id": sid},
                    )
                    await session.execute(
                        text(
                            "UPDATE auth_private.sessions SET cookie_hash=:cookie,encrypted=:encrypted,generation=generation+1,idle_until=:idle,access_until=:access WHERE id=:id"
                        ),
                        {
                            "cookie": digest(new_cookie),
                            "encrypted": encrypted,
                            "id": sid,
                            "idle": min(
                                row["expires_at"],
                                now() + timedelta(seconds=self.settings.AUTH_IDLE_SECONDS),
                            ),
                            "access": now() + timedelta(seconds=self.settings.AUTH_ACCESS_SECONDS),
                        },
                    )
                    await self._audit(session, row["usuario_id"], sid, "refresh")
        if failure is not None:
            raise failure
        return new_cookie

    async def _revoke(self, session: AsyncSession, sid: str, action: str) -> None:
        row = (
            (
                await session.execute(
                    text(
                        "UPDATE auth_private.sessions SET revoked_at=now() WHERE id=:id AND revoked_at IS NULL RETURNING encrypted,identity_id"
                    ),
                    {"id": sid},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            await session.execute(
                text(
                    "INSERT INTO auth_private.revocations(session_id,encrypted) VALUES(:id,:encrypted) ON CONFLICT DO NOTHING"
                ),
                {"id": sid, "encrypted": row["encrypted"]},
            )
            await self._audit(session, None, sid, action)

    async def logout(self, cookie: str, all_sessions: bool) -> None:
        async with AsyncSession(self.engine) as session, session.begin():
            row = await self._lookup(session, cookie, lock=True)
            ids = [row["id"]]
            if all_sessions:
                ids = list(
                    (
                        await session.execute(
                            text(
                                "SELECT id FROM auth_private.sessions WHERE identity_id=:identity ORDER BY id FOR UPDATE"
                            ),
                            {"identity": row["identity_id"]},
                        )
                    ).scalars()
                )
            for sid in ids:
                await self._revoke(session, str(sid), "logout")
        await self.drain_revocations()

    async def expire_sessions(self) -> None:
        async with AsyncSession(self.engine) as session, session.begin():
            ids = list(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM auth_private.sessions WHERE revoked_at IS NULL AND LEAST(expires_at,idle_until)<=now() ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED"
                        )
                    )
                ).scalars()
            )
            for sid in ids:
                await self._revoke(session, str(sid), "expiry")

    async def drain_revocations(self) -> None:
        async with AsyncSession(self.engine) as session, session.begin():
            rows = (
                await session.execute(
                    text(
                        "SELECT session_id,encrypted FROM auth_private.revocations WHERE retry_at<=now() ORDER BY session_id LIMIT 50 FOR UPDATE SKIP LOCKED"
                    )
                )
            ).mappings()
            for row in rows:
                sid = str(row["session_id"])
                credentials = json.loads(self.keys.open(row["encrypted"], "session:" + sid))
                try:
                    await self.oidc.revoke(credentials["refresh"])
                except IdentityError:
                    await session.execute(
                        text(
                            "UPDATE auth_private.revocations SET attempts=attempts+1,retry_at=now()+interval '1 minute' WHERE session_id=:id"
                        ),
                        {"id": sid},
                    )
                else:
                    await session.execute(
                        text("DELETE FROM auth_private.revocations WHERE session_id=:id"),
                        {"id": sid},
                    )

    @staticmethod
    async def _audit(session: AsyncSession, user: int | None, sid: str | None, action: str) -> None:
        await session.execute(
            text(
                "INSERT INTO auth_private.audit(usuario_id,session_id,action) VALUES(:user,:sid,:action)"
            ),
            {"user": user, "sid": sid, "action": action},
        )


@lru_cache
def identity_service() -> IdentityService:
    settings = identity_settings()
    if not settings.AUTH_ENABLED or settings.AUTH_DATABASE_URL is None:
        raise IdentityError("identity_not_configured", 503)
    engine = create_async_engine(
        settings.AUTH_DATABASE_URL.get_secret_value(),
        echo=False,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    return IdentityService(settings, engine)
