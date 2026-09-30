"""Required PostgreSQL acceptance, explicitly invoked by CI; no Docker/DB skips allowed."""

import asyncio
import os

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from bancaemdia.coleta.readers.reference import reference_registry
from bancaemdia.models import Aposta, Evento, ReaderQuarantine, Usuario
from bancaemdia.services.reader_capture import parse_or_quarantine, safe_evidence
from tests.coleta.test_reader_contract import envelope, source
from tests.conftest import ROOT


@pytest.fixture(scope="session", autouse=True)
def require_database():
    assert os.environ.get("TEST_DATABASE_URL"), (
        "mandatory acceptance requires PostgreSQL TEST_DATABASE_URL"
    )


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"raw_schema_version": "example-2"}, "schema_drift"),
        ({"envelope_schema": 2}, "schema_drift"),
        ({"content_type": "text/html"}, "schema_drift"),
        ({"hostname": "other.example.invalid"}, "wrong_host"),
    ],
)
async def test_drift_quarantines_once_without_creating_financial_rows(
    engine_app, como, novo_usuario, changes, code
):
    uid = await novo_usuario()
    value = envelope(**changes)
    async with como(engine_app, uid) as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Aposta, Evento)
        ]
        first = await parse_or_quarantine(session, uid, value, reference_registry())
        second = await parse_or_quarantine(session, uid, value, reference_registry())
        assert first.error_code == code and first.parsed is None
        assert first.quarantine_id == second.quarantine_id
        assert await session.scalar(select(func.count()).select_from(ReaderQuarantine)) == 1
        after = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Aposta, Evento)
        ]
        assert before == after
        row = await session.get(ReaderQuarantine, first.quarantine_id)
        assert row.reason == first.reason
        if changes.get("content_type") == "text/html":
            assert row.envelope is None
        else:
            assert row.envelope["content_hash"] == value["content_hash"]
        await session.commit()


async def test_unsafe_body_is_never_persisted_or_logged(engine_app, como, novo_usuario):
    from structlog.testing import capture_logs

    uid = await novo_usuario()
    value = envelope(
        source(note="Bearer synthetic-quarantine-sentinel"), raw_schema_version="future"
    )
    with capture_logs() as logs:
        async with como(engine_app, uid) as session:
            outcome = await parse_or_quarantine(session, uid, value, reference_registry())
            row = await session.get(ReaderQuarantine, outcome.quarantine_id)
            assert outcome.error_code == "unsafe_payload" and row.envelope is None
            assert "sentinel" not in str(row.reason)
            await session.commit()
    assert "sentinel" not in str(logs)
    assert any(log.get("error_code") == "unsafe_payload" for log in logs)


async def test_success_only_returns_backend_canonical_data(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    async with como(engine_app, uid) as session:
        outcome = await parse_or_quarantine(session, uid, envelope(), reference_registry())
        assert outcome.parsed.bets[0].stake_centavos == 1234
        assert outcome.quarantine_id is None
        for model in (Aposta, Evento, ReaderQuarantine):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_owner_rls_and_no_scope_hide_evidence(engine_app, como, novo_usuario):
    ana, bia = await novo_usuario(), await novo_usuario()
    async with como(engine_app, ana) as session:
        outcome = await parse_or_quarantine(
            session, ana, envelope(raw_schema_version="future"), reference_registry()
        )
        await session.commit()
    for uid in (bia, None):
        async with como(engine_app, uid) as session:
            assert await session.get(ReaderQuarantine, outcome.quarantine_id) is None
            with pytest.raises(PermissionError):
                await parse_or_quarantine(session, ana, envelope(), reference_registry())
    async with como(engine_app, ana) as session:
        assert await session.get(ReaderQuarantine, outcome.quarantine_id) is not None


async def test_foreign_tenant_insert_is_rejected_by_database(engine_app, como, novo_usuario):
    ana, bia = await novo_usuario(), await novo_usuario()
    async with como(engine_app, ana) as session:
        session.add(
            ReaderQuarantine(
                usuario_id=bia,
                capture_sha256="0" * 64,
                error_code="schema_drift",
                reason="unrecognized_schema",
            )
        )
        with pytest.raises(DBAPIError):
            await session.flush()


async def test_application_cannot_update_or_delete_quarantine(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    async with como(engine_app, uid) as session:
        outcome = await parse_or_quarantine(
            session, uid, envelope(raw_schema_version="future"), reference_registry()
        )
        await session.commit()
    async with como(engine_app, uid) as session:
        changed = await session.execute(
            update(ReaderQuarantine)
            .where(ReaderQuarantine.id == outcome.quarantine_id)
            .values(reason="reader_failure")
        )
        removed = await session.execute(
            delete(ReaderQuarantine).where(ReaderQuarantine.id == outcome.quarantine_id)
        )
        assert changed.rowcount == removed.rowcount == 0


async def test_inactive_tenant_is_rejected(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    async with como(engine_app, uid) as session:
        await session.execute(update(Usuario).where(Usuario.id == uid).values(ativo=False))
        await session.commit()
    async with como(engine_app, uid) as session:
        with pytest.raises(PermissionError):
            await parse_or_quarantine(session, uid, envelope(), reference_registry())


async def test_transaction_rollback_leaves_no_evidence_and_retry_converges(
    engine_app, como, novo_usuario
):
    uid = await novo_usuario()
    async with como(engine_app, uid) as session:
        outcome = await parse_or_quarantine(
            session, uid, envelope(raw_schema_version="future"), reference_registry()
        )
        assert outcome.quarantine_id
        await session.rollback()
    async with como(engine_app, uid) as session:
        assert await session.scalar(select(func.count()).select_from(ReaderQuarantine)) == 0
        assert (
            await parse_or_quarantine(
                session, uid, envelope(raw_schema_version="future"), reference_registry()
            )
        ).quarantine_id
        await session.commit()


async def test_concurrent_replays_share_one_evidence_row(engine_app, como, novo_usuario):
    uid = await novo_usuario()

    async def attempt():
        async with como(engine_app, uid) as session:
            result = await parse_or_quarantine(
                session, uid, envelope(raw_schema_version="future"), reference_registry()
            )
            await session.commit()
            return result.quarantine_id

    results = await asyncio.gather(*(attempt() for _ in range(5)))
    assert len(set(results)) == 1
    async with como(engine_app, uid) as session:
        assert await session.scalar(select(func.count()).select_from(ReaderQuarantine)) == 1


def test_unvalidated_evidence_is_omitted():
    assert safe_evidence({}) is None
    assert safe_evidence(envelope({"authorization": "synthetic"})) is None
    assert safe_evidence(envelope(content_type="unknown")) is None


async def test_rls_is_forced_and_only_select_insert_are_allowed(engine_admin):
    async with engine_admin.connect() as connection:
        flags = (
            await connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='reader_quarantine'"
                )
            )
        ).one()
        assert flags == (True, True)
        policies = (
            (
                await connection.execute(
                    text(
                        "SELECT cmd FROM pg_policies WHERE tablename='reader_quarantine' ORDER BY cmd"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert policies == ["INSERT", "SELECT"]


def migrate_table(connection, direction):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    module = ScriptDirectory.from_config(config).get_revision("c114reader2026").module
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def test_downgrade_refuses_existing_evidence(engine_admin, engine_app, como, novo_usuario):
    uid = await novo_usuario()
    async with como(engine_app, uid) as session:
        await parse_or_quarantine(
            session, uid, envelope(raw_schema_version="future"), reference_registry()
        )
        await session.commit()
    async with engine_admin.begin() as connection:
        with pytest.raises(DBAPIError, match="quarantine evidence exists"):
            async with connection.begin_nested():
                await connection.run_sync(lambda c: migrate_table(c, "downgrade"))
        assert await connection.scalar(text("SELECT count(*) FROM reader_quarantine")) > 0


async def test_empty_downgrade_upgrade_roundtrip_is_transactionally_reversible(engine_admin):
    async with engine_admin.connect() as connection:
        transaction = await connection.begin()
        try:
            # Disposable test DB only; rollback restores all preceding evidence and grants.
            await connection.execute(text("TRUNCATE reader_quarantine"))
            await connection.run_sync(lambda c: migrate_table(c, "downgrade"))
            assert await connection.scalar(text("SELECT to_regclass('reader_quarantine')")) is None
            await connection.run_sync(lambda c: migrate_table(c, "upgrade"))
            assert (
                await connection.scalar(
                    text(
                        "SELECT relforcerowsecurity FROM pg_class WHERE relname='reader_quarantine'"
                    )
                )
                is True
            )
        finally:
            await transaction.rollback()
