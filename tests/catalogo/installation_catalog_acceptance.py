"""Mandatory #107 composition: real pairing HTTP, JWT, installation token and RLS."""

import base64
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text

from bancaemdia.coleta.catalogo import Observation, Source, Technical, canonical, sha256
from bancaemdia.config import get_settings
from bancaemdia.models.casa_dominio import CasaDominio
from bancaemdia.services.catalogo import publish, synchronize, update_technical
from bancaemdia.services.catalogo_assinatura import CatalogSigner

PATH = "/api/v1/coleta/catalogo"


@pytest.fixture
async def installation_system(engine_app, engine_admin, novo_usuario, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "integration/coleta/test_pairing.py"
    assert path.exists(), "Pinned #107 integration is required; never skip installation acceptance"
    spec = importlib.util.spec_from_file_location("catalog_pairing_fixtures", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generator = module.system.__wrapped__(
        engine_app, engine_admin, novo_usuario, monkeypatch, module.signing_key.__wrapped__()
    )
    async for system in generator:
        async with engine_admin.begin() as conn:
            await conn.execute(
                text("INSERT INTO catalogo_operadores(usuario_id) VALUES (:uid)"),
                {"uid": system.user},
            )
        system.signer_key = Ed25519PrivateKey.generate()
        system.environment = get_settings().APP_ENV
        yield system


async def publication(system, *, ago=None, technical=None):
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(system.engine, expire_on_commit=False) as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(system.user)}
        )
        now = datetime.now(UTC)
        name = "HTTP-" + str(system.user)
        source = Source(
            source_key=name.lower(),
            kind="federal",
            jurisdiction="BR",
            url="https://www.gov.br/synthetic",
            consulted_at=now,
            valid_until=now + timedelta(days=7),
            snapshot_sha256=sha256(b"synthetic source"),
            observations=[Observation(brand=name, hostname="exact.bet.br", situation="autorizada")],
        )
        await synchronize(session, system.user, source, b"synthetic source", dry_run=False)
        row = await session.scalar(select(CasaDominio).where(CasaDominio.marca == name))
        if technical:
            await update_technical(session, system.user, row.id, technical)
        result = await publish(
            session,
            system.user,
            system.environment,
            CatalogSigner("http-test", system.signer_key),
            now=ago,
        )
        await session.commit()
        return result


def params(system, **changes):
    return {"client_version": "1.0.0", "environment": system.environment, **changes}


async def test_real_pairing_gets_signed_exact_catalog_and_conditional_cache(installation_system):
    s = installation_system
    pair = await s.pair()
    row = await publication(s)
    headers = {"X-Coleta-Token": pair["token"]}
    response = await s.http.get(PATH, params=params(s), headers=headers)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert response.content == canonical(envelope)
    s.signer_key.public_key().verify(
        base64.urlsafe_b64decode(envelope["signature"] + "=="), canonical(envelope["payload"])
    )
    assert envelope["payload"]["catalog_version"] == row.id
    assert envelope["payload"]["environment"] == s.environment
    assert envelope["payload"]["last_known_good"]["maximum_age_seconds"] == 86400
    assert not envelope["payload"]["last_known_good"]["new_hosts_allowed"]
    assert all("*" not in e["hostname"] for e in envelope["payload"]["entries"])
    assert pair["token"] not in response.text and "usuario_id" not in response.text
    assert response.headers["Cache-Control"].startswith("private, max-age=")
    conditional = await s.http.get(
        PATH, params=params(s), headers={**headers, "If-None-Match": response.headers["ETag"]}
    )
    assert conditional.status_code == 304 and not conditional.content
    assert conditional.headers["ETag"] == response.headers["ETag"]


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"environment": "wrong-environment"}, 409),
        ({"known_version": 2**53 - 1}, 409),
        ({"client_version": "0.9.9"}, 426),
        ({"client_version": "01.0.0"}, 426),
        ({"client_version": "1.0.0-beta"}, 422),
    ],
)
async def test_environment_downgrade_client_rules_before_cache(
    installation_system, changes, status
):
    s = installation_system
    pair = await s.pair()
    row = await publication(s)
    response = await s.http.get(
        PATH,
        params=params(s, **changes),
        headers={"X-Coleta-Token": pair["token"], "If-None-Match": f'"{row.etag}"'},
    )
    assert response.status_code == status, response.text


async def test_stale_publication_refuses_304_and_cannot_extend_offline_bounds(installation_system):
    s = installation_system
    pair = await s.pair()
    row = await publication(s, ago=datetime.now(UTC) - timedelta(hours=2))
    response = await s.http.get(
        PATH,
        params=params(s),
        headers={"X-Coleta-Token": pair["token"], "If-None-Match": f'"{row.etag}"'},
    )
    assert response.status_code == 503


async def test_revocation_and_rotation_are_checked_even_with_valid_etag(installation_system):
    s = installation_system
    pair = await s.pair()
    row = await publication(s)
    headers = {"X-Coleta-Token": pair["token"], "If-None-Match": f'"{row.etag}"'}
    rotated = await s.http.post(
        f"/api/v1/coleta/installations/{pair['instalacao_id']}/rotate", headers=s.headers()
    )
    assert rotated.status_code == 200, rotated.text
    assert (await s.http.get(PATH, params=params(s), headers=headers)).status_code == 403
    new_headers = {"X-Coleta-Token": rotated.json()["token"]}
    assert (await s.http.get(PATH, params=params(s), headers=new_headers)).status_code == 200
    revoked = await s.http.delete(
        f"/api/v1/coleta/installations/{pair['instalacao_id']}", headers=s.headers()
    )
    assert revoked.status_code == 204, revoked.text
    assert (await s.http.get(PATH, params=params(s), headers=new_headers)).status_code == 403


@pytest.mark.parametrize(
    "query",
    ["token=secret", "client_version=1.0.0&client_version=2.0.0", "environment=" + "a" * 41],
)
async def test_catalog_rejects_unknown_duplicate_or_unbounded_query_without_cache(
    installation_system, query
):
    s = installation_system
    pair = await s.pair()
    response = await s.http.get(PATH + "?" + query, headers={"X-Coleta-Token": pair["token"]})
    assert response.status_code == 400 and response.headers["Cache-Control"] == "no-store"
    assert pair["token"] not in response.text and "secret" not in response.text


async def test_catalog_never_accepts_credentials_over_http(installation_system):
    s = installation_system
    pair = await s.pair()
    response = await s.http.get(
        "http://api.test" + PATH, params=params(s), headers={"X-Coleta-Token": pair["token"]}
    )
    assert response.status_code == 400 and response.headers["Cache-Control"] == "no-store"
    assert pair["token"] not in response.text


async def test_retrieval_uses_installation_credentials_not_bearer_or_legacy_token(
    installation_system,
):
    s = installation_system
    await publication(s)
    assert (await s.http.get(PATH, params=params(s), headers=s.headers())).status_code == 403
    assert (
        await s.http.get(PATH, params=params(s), headers={"X-Coleta-Token": "invented-token"})
    ).status_code == 403


async def test_revoked_exact_host_remains_signed_tombstone_and_preserves_old_catalog(
    installation_system,
):
    s = installation_system
    pair = await s.pair()
    first = await publication(
        s,
        technical=Technical(
            support="suportado",
            rollout="enabled",
            adapter="synthetic",
            adapter_version="1.0.0",
            capture_evidence_sha256=sha256(b"capture"),
        ),
    )
    second = await publication(s, technical=Technical(support="regressao", rollout="revoked"))
    response = await s.http.get(
        PATH, params=params(s, known_version=first.id), headers={"X-Coleta-Token": pair["token"]}
    )
    assert response.status_code == 200, response.text
    entry = next(
        e for e in response.json()["payload"]["entries"] if e["brand"] == "HTTP-" + str(s.user)
    )
    assert entry["hostname"] == "exact.bet.br" and entry["rollout"] == "revoked"
    assert response.json()["payload"]["catalog_version"] == second.id
    old_entry = next(
        e for e in first.envelope["payload"]["entries"] if e["brand"] == "HTTP-" + str(s.user)
    )
    assert old_entry["rollout"] == "enabled"
