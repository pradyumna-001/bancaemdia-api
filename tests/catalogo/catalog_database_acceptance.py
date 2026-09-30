"""Real migrations, PostgreSQL constraints/RLS, transactions, audit and publication."""

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from bancaemdia.coleta.catalogo import Observation, Source, Technical, sha256
from bancaemdia.models.casa_dominio import (
    CasaDominio,
    CatalogoAuditoria,
    CatalogoConfirmacao,
    CatalogoPublicacao,
    CatalogoSnapshot,
)
from bancaemdia.services.catalogo import (
    admin_export,
    include_manual_candidates,
    publish,
    record_redirect,
    synchronize,
    update_technical,
)
from bancaemdia.services.catalogo_assinatura import CatalogSigner


def source(system, observations=None, raw=b"snapshot", **changes):
    now = datetime.now(UTC)
    return Source(**{
        "source_key": "synthetic-" + system.brand.lower(),
        "kind": "federal",
        "jurisdiction": "BR",
        "url": "https://www.gov.br/synthetic",
        "consulted_at": now,
        "valid_until": now + timedelta(days=7),
        "snapshot_sha256": sha256(raw),
        "observations": observations
        or [Observation(brand=system.brand, hostname="exact.bet.br", situation="autorizada")],
        **changes,
    })


async def test_dry_run_persists_nothing_and_apply_is_idempotent(catalog_system):
    s = catalog_system
    observed = source(s)
    async with s.como(s.engine, s.user) as session:
        report = await synchronize(session, s.user, observed, b"snapshot")
        assert report["added"] == [s.brand.upper() + "|exact.bet.br"]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CatalogoSnapshot)
                .where(CatalogoSnapshot.source_key == observed.source_key)
            )
            == 0
        )
        await session.rollback()
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, observed, b"snapshot", dry_run=False)
        await session.commit()
    async with s.como(s.engine, s.user) as session:
        assert (await synchronize(session, s.user, observed, b"snapshot", dry_run=False))[
            "idempotent"
        ]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CatalogoSnapshot)
                .where(CatalogoSnapshot.source_key == observed.source_key)
            )
            == 1
        )
        entry = await session.scalar(
            select(CasaDominio).where(CasaDominio.marca == s.brand.upper())
        )
        assert entry.tecnico["support"] == "nao_avaliado" and entry.tecnico["rollout"] == "disabled"


async def test_source_change_removal_redirect_preserves_history_and_support(catalog_system):
    s = catalog_system
    original = source(s)
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, original, b"snapshot", dry_run=False)
        row = await session.scalar(select(CasaDominio).where(CasaDominio.marca == s.brand.upper()))
        await update_technical(session, s.user, row.id, Technical(support="precisa_captura"))
        await session.commit()
    changed = source(
        s, [Observation(brand=s.brand, hostname="new.bet.br", situation="autorizada")], b"changed"
    )
    async with s.como(s.engine, s.user) as session:
        report = await synchronize(session, s.user, changed, b"changed", dry_run=False)
        assert report["removed_from_source"] == [s.brand.upper() + "|exact.bet.br"]
        old = await session.scalar(
            select(CasaDominio).where(
                CasaDominio.marca == s.brand.upper(), CasaDominio.hostname == "exact.bet.br"
            )
        )
        new = await session.scalar(
            select(CasaDominio).where(
                CasaDominio.marca == s.brand.upper(), CasaDominio.hostname == "new.bet.br"
            )
        )
        assert old.evidencias[changed.source_key]["situation"] == "removida_da_fonte"
        assert (
            old.tecnico["support"] == "precisa_captura" and new.tecnico["support"] == "nao_avaliado"
        )
        await update_technical(
            session,
            s.user,
            new.id,
            Technical(
                aliases=["exact.bet.br"],
                redirect_chain=["https://exact.bet.br/", "https://new.bet.br/"],
            ),
        )
        assert new.tecnico["aliases"] == ["exact.bet.br"]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CatalogoSnapshot)
                .where(CatalogoSnapshot.source_key == changed.source_key)
            )
            == 2
        )
        await session.commit()


@pytest.mark.parametrize(
    "kind,situation",
    [
        ("judicial", "judicial"),
        ("applicants", "solicitante"),
        ("state", "autorizada"),
        ("state", "revogada"),
        ("state", "suspensa"),
        ("state", "expirada"),
    ],
)
async def test_evidence_classes_never_change_technical_state(catalog_system, kind, situation):
    s = catalog_system
    evidence = source(
        s,
        [Observation(brand=s.brand, hostname="exact.bet.br", situation=situation)],
        kind=kind,
        jurisdiction="PR" if kind == "state" else "BR",
    )
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, evidence, b"snapshot", dry_run=False)
        entry = await session.scalar(
            select(CasaDominio).where(CasaDominio.marca == s.brand.upper())
        )
        assert entry.evidencias[evidence.source_key]["kind"] == kind
        assert entry.tecnico == Technical().model_dump()
        await session.commit()


async def test_manual_confirmation_is_owned_idempotent_and_no_secrets(catalog_system):
    s = catalog_system
    body = {
        "brand": s.brand,
        "hostname": "manual.bet.br",
        "access_confirmed": True,
        "confirmed_at": datetime.now(UTC).isoformat(),
        "evidence_sha256": sha256(b"synthetic proof"),
    }
    for _ in range(2):
        response = await s.http.post(
            "/api/v1/catalogo/candidatos", headers=s.headers(s.other), json=body
        )
        assert response.status_code == 200, response.text
        assert response.json()["situation"] == "acesso_confirmado"
    async with s.como(s.engine, s.user) as session:
        keys = await include_manual_candidates(session, s.user, dry_run=False)
        assert s.brand.upper() + "|manual.bet.br" in keys
        row = await session.scalar(select(CasaDominio).where(CasaDominio.marca == s.brand.upper()))
        assert row.tecnico == Technical().model_dump()
        assert next(iter(row.evidencias.values()))["situation"] == "acesso_confirmado"
        await session.commit()
    async with s.como(s.engine, s.other) as session:
        confirmations = list((await session.scalars(select(CatalogoConfirmacao))).all())
        assert len(confirmations) == 1 and confirmations[0].usuario_id == s.other
        assert await session.scalar(select(func.count()).select_from(CasaDominio)) == 0
        assert await session.scalar(select(func.count()).select_from(CatalogoSnapshot)) == 0
    forbidden = await s.http.post(
        "/api/v1/catalogo/candidatos",
        headers=s.headers(s.other),
        json={**body, "token": "SENTINEL-SECRET"},
    )
    assert forbidden.status_code == 422


@pytest.mark.parametrize(
    "body_change",
    [{"access_confirmed": False}, {"hostname": "*.bet.br"}, {"hostname": "https://secret@bet.br/"}],
)
async def test_manual_confirmation_refuses_unconfirmed_or_non_exact_hosts(
    catalog_system, body_change
):
    s = catalog_system
    response = await s.http.post(
        "/api/v1/catalogo/candidatos",
        headers=s.headers(s.other),
        json={
            "brand": s.brand,
            "hostname": "exact.bet.br",
            "access_confirmed": True,
            "confirmed_at": datetime.now(UTC).isoformat(),
            "evidence_sha256": sha256(b"proof"),
            **body_change,
        },
    )
    assert response.status_code == 400


async def test_rls_cannot_self_grant_operator_or_read_foreign_confirmations(catalog_system):
    s = catalog_system
    async with s.como(s.engine, s.other) as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("INSERT INTO catalogo_operadores(usuario_id) VALUES (:uid)"), {"uid": s.other}
            )
        await session.rollback()
    for path in ("/api/v1/admin/casas", "/api/v1/admin/casas/export"):
        response = await s.http.get(path, headers=s.headers(s.other))
        assert response.status_code == 403
        response = await s.http.get(path, headers=s.headers())
        assert response.status_code == 200, response.text
        assert response.json()["totals"]["state_jurisdictions"] == 27
        assert (
            response.json()["totals"]["configured_states"]
            + len(response.json()["totals"]["unconfigured_states"])
            == 27
        )
    async with s.como(s.engine, s.other) as session:
        with pytest.raises(DBAPIError):
            session.add(
                CatalogoConfirmacao(
                    usuario_id=s.user,
                    marca=s.brand,
                    hostname="exact.bet.br",
                    confirmado_em=datetime.now(UTC),
                    evidence_sha256=sha256(b"proof"),
                )
            )
            await session.flush()
        await session.rollback()


async def test_transaction_rollback_restores_catalog_sources_and_audit(catalog_system):
    s = catalog_system
    observed = source(s)
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, observed, b"snapshot", dry_run=False)
        await session.rollback()
    async with s.como(s.engine, s.user) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CasaDominio)
                .where(CasaDominio.marca == s.brand.upper())
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CatalogoSnapshot)
                .where(CatalogoSnapshot.source_key == observed.source_key)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CatalogoAuditoria)
                .where(CatalogoAuditoria.usuario_id == s.user)
            )
            == 0
        )


async def test_real_concurrent_sync_converges_without_duplicate_snapshot(catalog_system):
    s = catalog_system
    observed = source(s)

    async def apply():
        async with s.como(s.engine, s.user) as session:
            result = await synchronize(session, s.user, observed, b"snapshot", dry_run=False)
            await session.commit()
            return result

    reports = await asyncio.gather(apply(), apply())
    assert sum(bool(r.get("idempotent")) for r in reports) == 1
    async with s.como(s.engine, s.user) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CatalogoSnapshot)
                .where(CatalogoSnapshot.source_key == observed.source_key)
            )
            == 1
        )


@pytest.mark.parametrize(
    "table",
    ["catalogo_snapshots", "catalogo_publicacoes", "catalogo_confirmacoes", "catalogo_auditoria"],
)
async def test_history_is_append_only_even_for_database_owner(catalog_system, table):
    s = catalog_system
    observed = source(s)
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, observed, b"snapshot", dry_run=False)
        await publish(
            session, s.user, s.environment, CatalogSigner("test", Ed25519PrivateKey.generate())
        )
        session.add(
            CatalogoConfirmacao(
                usuario_id=s.user,
                marca=s.brand,
                hostname="exact.bet.br",
                confirmado_em=datetime.now(UTC),
                evidence_sha256=sha256(b"proof"),
            )
        )
        await session.commit()
    async with s.admin.connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(text(f"DELETE FROM {table}"))
        await conn.rollback()


async def test_publication_monotonic_signed_and_readable_without_regulatory_data(catalog_system):
    s = catalog_system
    key = Ed25519PrivateKey.generate()
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, source(s), b"snapshot", dry_run=False)
        one = await publish(session, s.user, s.environment, CatalogSigner("test", key))
        two = await publish(session, s.user, s.environment, CatalogSigner("test", key))
        assert two.id > one.id
        assert all(
            "evidence" not in entry and "evidencias" not in entry and "company" not in entry
            for entry in two.envelope["payload"]["entries"]
        )
        assert "usuario_id" not in str(two.envelope) and "token" not in str(two.envelope)
        await session.commit()
    async with s.como(s.engine, s.other) as session:
        assert await session.get(CatalogoPublicacao, two.id) is not None
        assert await session.scalar(select(func.count()).select_from(CasaDominio)) == 0


async def test_no_hash_mismatch_or_stale_source_can_mutate_catalog(catalog_system):
    s = catalog_system
    original = source(s)
    async with s.como(s.engine, s.user) as session:
        with pytest.raises(ValueError, match="hash"):
            await synchronize(session, s.user, original, b"tampered", dry_run=False)
        await synchronize(session, s.user, original, b"snapshot", dry_run=False)
        await session.commit()
    async with s.como(s.engine, s.user) as session:
        stale = original.model_copy(
            update={
                "consulted_at": original.consulted_at - timedelta(days=1),
                "snapshot_sha256": sha256(b"stale"),
            }
        )
        with pytest.raises(ValueError, match="backwards"):
            await synchronize(session, s.user, stale, b"stale", dry_run=False)


async def test_operator_authorization_is_not_a_claim_or_user_choice(catalog_system):
    s = catalog_system
    async with s.como(s.engine, s.other) as session:
        with pytest.raises(HTTPException) as error:
            await admin_export(session, s.other)
        assert error.value.status_code == 403
        with pytest.raises(HTTPException):
            await publish(
                session, s.other, s.environment, CatalogSigner("test", Ed25519PrivateKey.generate())
            )


async def test_redirect_creates_final_exact_host_with_aliases_and_no_support_promotion(
    catalog_system,
):
    s = catalog_system
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, source(s), b"snapshot", dry_run=False)
        old = await session.scalar(select(CasaDominio).where(CasaDominio.marca == s.brand.upper()))
        await update_technical(
            session,
            s.user,
            old.id,
            Technical(
                support="suportado",
                rollout="enabled",
                adapter="synthetic",
                adapter_version="1.0.0",
                capture_evidence_sha256=sha256(b"capture"),
            ),
        )
        target = await record_redirect(
            session, s.user, old.id, ["https://exact.bet.br/", "https://new.bet.br/"]
        )
        assert target.hostname == "new.bet.br" and target.tecnico["aliases"] == ["exact.bet.br"]
        assert (
            target.tecnico["support"] == "nao_avaliado" and target.tecnico["rollout"] == "disabled"
        )
        assert target.evidencias == {} and old.evidencias
        assert old.tecnico["support"] == "suportado" and old.tecnico["rollout"] == "disabled"
        await session.commit()


async def test_new_consultation_cannot_reinterpret_identical_snapshot(catalog_system):
    s = catalog_system
    original = source(s)
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, original, b"snapshot", dry_run=False)
        await session.commit()
    changed = original.model_copy(
        update={
            "consulted_at": original.consulted_at + timedelta(seconds=1),
            "observations": [
                Observation(brand=s.brand, hostname="forged.bet.br", situation="autorizada")
            ],
        }
    )
    async with s.como(s.engine, s.user) as session:
        with pytest.raises(ValueError, match="identical snapshot"):
            await synchronize(session, s.user, changed, b"snapshot", dry_run=False)


@pytest.mark.parametrize("role", ["admin", "engine"])
async def test_downgrade_cannot_erase_history_hidden_by_rls(catalog_system, role):
    s = catalog_system
    async with s.como(s.engine, s.user) as session:
        await synchronize(session, s.user, source(s), b"snapshot", dry_run=False)
        await session.commit()
    path = (
        Path(__file__).resolve().parents[2] / "alembic/versions/c113catalog2026_catalogo_casas.py"
    )
    spec = importlib.util.spec_from_file_location("catalog_downgrade_test", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def attempt(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()

    async with getattr(s, role).connect() as conn:
        with pytest.raises(DBAPIError):
            await conn.run_sync(attempt)
        await conn.rollback()
    async with s.como(s.engine, s.user) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CatalogoSnapshot)
                .where(CatalogoSnapshot.source_key == "synthetic-" + s.brand.lower())
            )
            == 1
        )
