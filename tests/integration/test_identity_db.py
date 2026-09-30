import asyncio
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.auth.identity_service import IdentityService
from bancaemdia.auth.oidc import IdentityError
from tests.identity.support import key_settings


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
        # This delivery has no billing schema, trial write or subscription side effect.
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('assinaturas','subscriptions','trials')"
                )
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
