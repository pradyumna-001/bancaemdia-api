import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import jwt.api_jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.auth import identity_crypto, identity_service
from bancaemdia.auth.identity_service import IdentityService
from bancaemdia.auth.oidc import IdentityError
from tests.identity.support import key_settings

# These tests share the PostgreSQL fixture with exclusive offline replay tests.
pytestmark = pytest.mark.xdist_group("postgres")


@pytest.fixture
async def identity_service_db(banco, engine_admin, tmp_path):
    role = "identity_test_" + uuid4().hex
    password = uuid4().hex
    async with engine_admin.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS"))
        await conn.execute(text(f"GRANT bancaemdia_auth TO {role}"))
    url = (
        make_url(banco.url_admin)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(url, hide_parameters=True, poolclass=NullPool)
    settings = key_settings(
        tmp_path,
        AUTH_DATABASE_URL=SecretStr(url),
        OIDC_ISSUER="https://issuer.example.org",
        OIDC_CLIENT_ID="test",
    )
    yield IdentityService(settings, engine)
    await engine.dispose()
    async with engine_admin.begin() as conn:
        await conn.execute(text(f"DROP ROLE {role}"))


def identity_claims(**changes):
    return {
        "iss": "https://issuer.example.org",
        "sub": uuid4().hex,
        "email_verified": True,
        "email": uuid4().hex + "@example.org",
        "name": "Disposable",
        **changes,
    }


async def provision(service, claims):
    async with AsyncSession(service.engine) as session, session.begin():
        return await service.provision(session, claims)


async def test_repeated_concurrent_provision_has_one_proven_numeric_user(
    identity_service_db, engine_admin
):
    service = identity_service_db
    claims = identity_claims()
    linked = await asyncio.gather(*(provision(service, claims) for _ in range(12)))
    assert len(set(linked)) == 1
    user, identity = linked[0]
    assert isinstance(user, int) and user > 0 and identity != claims["sub"]
    async with engine_admin.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM usuarios WHERE email=:email"), {"email": claims["email"]}
            )
            == 1
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM auth_private.audit WHERE usuario_id=:id AND action='provision'"
                ),
                {"id": user},
            )
            == 1
        )
        # Billing exists on main, but its rollout has not been activated. Provisioning
        # must not create or confirm a trial as a side effect of verified registration.
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM assinaturas WHERE usuario_id=:id"), {"id": user}
            )
            == 0
        )


async def test_email_collision_never_links_another_external_subject(identity_service_db):
    claims = identity_claims()
    first = await provision(identity_service_db, claims)
    with pytest.raises(IdentityError, match="identity_conflict") as reason:
        await provision(identity_service_db, {**claims, "sub": uuid4().hex})
    assert reason.value.status == 409
    assert await provision(identity_service_db, claims) == first


@pytest.mark.parametrize(
    "change",
    [{"email_verified": False}, {"email_verified": "true"}, {"iss": "https://other.example.org"}],
)
async def test_unconfirmed_or_disallowed_identity_is_not_provisioned(identity_service_db, change):
    with pytest.raises(IdentityError):
        await provision(identity_service_db, identity_claims(**change))


async def test_inactive_link_cannot_be_reactivated_by_login(identity_service_db, engine_admin):
    claims = identity_claims()
    user, _ = await provision(identity_service_db, claims)
    async with engine_admin.begin() as conn:
        await conn.execute(text("UPDATE usuarios SET ativo=false WHERE id=:id"), {"id": user})
    with pytest.raises(IdentityError, match="account_inactive"):
        await provision(identity_service_db, claims)


async def test_ordinary_api_role_has_no_private_identity_or_token_access(
    engine_app, engine_admin, identity_service_db
):
    await identity_service_db.verify_database_role()
    async with engine_app.connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT encrypted FROM auth_private.sessions"))
    async with identity_service_db.engine.connect() as conn:
        assert not await conn.scalar(
            text("SELECT has_table_privilege(current_user,'auth_private.identities','UPDATE')")
        )
        assert not await conn.scalar(
            text("SELECT has_table_privilege(current_user,'auth_private.audit','DELETE')")
        )
        assert not await conn.scalar(
            text("SELECT has_table_privilege(current_user,'public.apostas','SELECT')")
        )
        assert not await conn.scalar(
            text("SELECT has_table_privilege(current_user,'public.usuarios','UPDATE')")
        )
        assert not await conn.scalar(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user")
        )


async def test_startup_refuses_database_owner_as_identity_credential(
    identity_service_db, engine_admin
):
    unsafe = IdentityService(identity_service_db.settings, engine_admin)
    with pytest.raises(ValueError, match="limited, separate auth role"):
        await unsafe.verify_database_role()


class FrozenClock:
    """One controllable instant for the service, the internal JWT issuer and PyJWT's exp check."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, start: float) -> None:
        self.at = start
        clock = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return datetime.fromtimestamp(clock.at, tz)

        monkeypatch.setattr(identity_service, "now", lambda: datetime.fromtimestamp(self.at, UTC))
        monkeypatch.setattr(identity_crypto, "time", SimpleNamespace(time=lambda: self.at))
        monkeypatch.setattr(jwt.api_jwt, "datetime", Frozen)


class FakeIssuer:
    """Stands in for the OIDC exchange only; sessions, keys, JWT and database are the real ones."""

    def __init__(self, claims):
        self.claims, self.state = claims, ""

    async def authorization(self, state, nonce, verifier):
        self.state = state
        return "https://issuer.example.org/auth"

    async def tokens(self, _form):
        return {"id_token": "external", "refresh_token": uuid4().hex}

    async def identity(self, _id_token, _nonce):
        return self.claims


async def test_access_deadline_is_one_instant_for_session_status_and_resources(
    identity_service_db, monkeypatch
):
    # The internal JWT's exp is whole seconds. If access_until kept the microseconds of a
    # separate now(), resources answered 401 access_expired for up to ~1 s while
    # /auth/session still said refresh_required=false, and the SPA never renewed.
    service = identity_service_db
    seconds = service.settings.AUTH_ACCESS_SECONDS
    service.oidc = FakeIssuer(identity_claims())
    clock = FrozenClock(monkeypatch, int(time.time()) + 0.9)

    async def assert_one_deadline(cookie, issued_at):
        status = await service.status(cookie)
        deadline = status["access_expires_at"].timestamp()
        clock.at = deadline - 0.5
        # Whatever /auth/session says, the resources must agree with it.
        assert (await service.status(cookie))["refresh_required"] is False
        assert await service.authenticate(cookie) == status["usuario_id"]
        clock.at = deadline  # The exact instant PyJWT stops accepting the token (exp <= now).
        assert (await service.status(cookie))["refresh_required"] is True
        with pytest.raises(IdentityError, match="access_expired"):
            await service.authenticate(cookie)
        assert deadline == int(issued_at) + seconds  # Same whole second as the JWT's exp.

    issued_at = clock.at
    _, browser = await service.begin("/signed-in")
    cookie, _ = await service.callback(service.oidc.state, browser, "code")
    await assert_one_deadline(cookie, issued_at)

    clock.at = issued_at = int(clock.at) + 0.9
    renewed = await service.refresh(cookie)
    await assert_one_deadline(renewed, issued_at)
