"""Mandatory real-PostgreSQL boundary for the candidate, not financial support proof."""

import asyncio
import os

import pytest
from sqlalchemy import func, select

from bancaemdia.coleta.readers.base import digest
from bancaemdia.coleta.readers.one_win import V1_SCHEMA, candidate_registry
from bancaemdia.coleta.readers.reference import reference_registry
from bancaemdia.coleta.readers.registry import ReaderRegistry
from bancaemdia.models import Aposta, Evento, ReaderQuarantine
from bancaemdia.services.reader_capture import parse_or_quarantine
from tests.coleta.test_one_win import envelope, source
from tests.coleta.test_reader_contract import envelope as reference_envelope


@pytest.fixture(scope="session", autouse=True)
def require_database():
    assert os.environ.get("TEST_DATABASE_URL"), "1win acceptance requires real PostgreSQL"


async def assert_no_money(session):
    for model in (Aposta, Evento):
        assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("status", range(5))
async def test_states_and_replay_retain_once_without_financial_rows(
    engine_app, como, novo_usuario, status
):
    uid = await novo_usuario()
    raw = source()
    raw["bet"]["status"] = status
    raw["bet"]["profitAmount"] = 30.85
    value = envelope(raw)
    async with como(engine_app, uid) as session:
        first = await parse_or_quarantine(session, uid, value, candidate_registry())
        await session.commit()
    async with como(engine_app, uid) as session:
        repeated = await parse_or_quarantine(session, uid, value, candidate_registry())
        assert first.quarantine_id == repeated.quarantine_id
        assert repeated.parsed is None
        assert repeated.reason == "financial_evidence_pending"
        row = await session.get(ReaderQuarantine, repeated.quarantine_id)
        assert row.envelope["content_hash"] == digest(value["payload_text"].encode())
        assert await session.scalar(select(func.count()).select_from(ReaderQuarantine)) == 1
        await assert_no_money(session)
        await session.commit()


@pytest.mark.parametrize("field", ["startAt", "competitors", "name"])
async def test_missing_game_or_selection_cannot_assign_account(
    engine_app, como, novo_usuario, field
):
    uid = await novo_usuario()
    raw = source()
    selection = raw["selections"][0]
    del selection["odd" if field == "name" else "match"][field]
    async with como(engine_app, uid) as session:
        outcome = await parse_or_quarantine(session, uid, envelope(raw), candidate_registry())
        assert outcome.reason == (
            "game_time_missing" if field == "startAt" else "selection_description_missing"
        )
        await assert_no_money(session)


async def test_legacy_projection_reports_missing_game_fields(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    raw = source()["bet"]
    del raw["betType"]
    async with como(engine_app, uid) as session:
        outcome = await parse_or_quarantine(
            session, uid, envelope(raw, raw_schema_version=V1_SCHEMA), candidate_registry()
        )
        assert outcome.reason == "game_fields_not_projected"
        await assert_no_money(session)


async def test_private_fields_are_never_persisted_or_logged(engine_app, como, novo_usuario):
    from structlog.testing import capture_logs

    uid = await novo_usuario()
    raw = source()
    raw["bet"]["authorization"] = "unit-secret-one-win-sentinel"
    with capture_logs() as logs:
        async with como(engine_app, uid) as session:
            outcome = await parse_or_quarantine(session, uid, envelope(raw), candidate_registry())
            row = await session.get(ReaderQuarantine, outcome.quarantine_id)
            assert outcome.error_code == "unsafe_payload" and row.envelope is None
            await assert_no_money(session)
            await session.commit()
    assert "unit-secret-one-win-sentinel" not in str(logs)


@pytest.mark.parametrize("scope", ["other", "none"])
async def test_evidence_is_tenant_scoped(engine_app, como, novo_usuario, scope):
    ana, bia = await novo_usuario(), await novo_usuario()
    async with como(engine_app, ana) as session:
        outcome = await parse_or_quarantine(session, ana, envelope(), candidate_registry())
        await session.commit()
    async with como(engine_app, bia if scope == "other" else None) as session:
        assert await session.get(ReaderQuarantine, outcome.quarantine_id) is None
        with pytest.raises(PermissionError):
            await parse_or_quarantine(session, ana, envelope(), candidate_registry())


async def test_concurrent_duplicate_capture_converges(engine_app, como, novo_usuario):
    uid = await novo_usuario()

    async def attempt():
        async with como(engine_app, uid) as session:
            outcome = await parse_or_quarantine(session, uid, envelope(), candidate_registry())
            await session.commit()
            return outcome.quarantine_id

    assert len(set(await asyncio.gather(*(attempt() for _ in range(5))))) == 1
    async with como(engine_app, uid) as session:
        assert await session.scalar(select(func.count()).select_from(ReaderQuarantine)) == 1
        await assert_no_money(session)


async def test_failure_does_not_disable_an_independent_reader(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    registry = ReaderRegistry(
        candidate_registry().registrations + reference_registry().registrations
    )
    async with como(engine_app, uid) as session:
        failed = await parse_or_quarantine(session, uid, envelope(), registry)
        passed = await parse_or_quarantine(session, uid, reference_envelope(), registry)
        assert failed.reason == "financial_evidence_pending"
        assert passed.parsed.bets[0].stake_centavos == 1234
        assert passed.quarantine_id is None
        await assert_no_money(session)
