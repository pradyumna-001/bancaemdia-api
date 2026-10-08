"""Mandatory PostgreSQL proof of exact native money, accounts, RLS and replay."""

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from bancaemdia.coleta.readers.base import ReaderEnvelope
from bancaemdia.coleta.readers.one_win import OneWinReader
from bancaemdia.models import Aposta, Casa, NativeAccount, NativeBet, NativeBetEvidence
from bancaemdia.services.native_financials import materialize_native, native_summary
from tests.coleta.test_native_money import native_source
from tests.coleta.test_one_win import envelope
from tests.integration.coleta import test_pairing as pairing_acceptance

signing_key = pairing_acceptance.signing_key
system = pairing_acceptance.system
pytestmark = pytest.mark.xdist_group("postgres")


@pytest.fixture(scope="session", autouse=True)
def require_database():
    if not os.environ.get("TEST_DATABASE_URL"):
        from testcontainers.core.docker_client import DockerClient

        assert DockerClient().client.ping(), "native acceptance requires real PostgreSQL"


def captured(raw=None, **changes):
    value = ReaderEnvelope.model_validate(
        envelope(native_source() if raw is None else raw, **changes)
    )
    return value, OneWinReader().parse_native(value)


async def house(session):
    await session.execute(
        insert(Casa).values(nome="1win").on_conflict_do_nothing(index_elements=["nome"])
    )
    return await session.scalar(select(Casa.id).where(Casa.nome == "1win"))


async def account(session, uid, house_id, **changes):
    values = {
        "usuario_id": uid,
        "casa_id": house_id,
        "currency": "USDT",
        "label": "Synthetic account",
        "valid_from": datetime(2025, 1, 1, tzinfo=UTC),
        "active": True,
    }
    values.update(changes)
    row = NativeAccount(**values)
    session.add(row)
    await session.flush()
    return row


async def prepare(engine, como, uid, **changes):
    async with como(engine, uid) as session:
        hid = await house(session)
        row = await account(session, uid, hid, **changes)
        await session.commit()
        return hid, row.id


@pytest.fixture
async def native_http(system, como, monkeypatch):
    from bancaemdia.coleta.readers import native_admission
    from bancaemdia.config import get_settings
    from bancaemdia.db.session import get_db, get_db_primary_snapshot
    from bancaemdia.main import app

    monkeypatch.setitem(
        app.dependency_overrides, get_db_primary_snapshot, app.dependency_overrides[get_db]
    )

    hid, aid = await prepare(system.engine, como, system.user)
    monkeypatch.setattr(get_settings(), "ONE_WIN_NATIVE_ENABLED", True)
    # Test-only admission: no real corpus approval is invented or published.
    monkeypatch.setattr(native_admission, "REVIEWED_BUNDLE_SHA256", "a" * 64)
    system.house_id = hid
    system.account_id = aid
    system.credential = await system.pair()
    system.capture_headers = {"X-Coleta-Token": system.credential["token"]}
    system.capture_body = {
        "transport_contract": "reader-capture-1",
        "envelope": captured()[0].model_dump(mode="json"),
    }
    return system


async def test_native_http_lossless_capture_report_and_replay(native_http):
    system = native_http
    for expected in ("created", "noop"):
        response = await system.http.post(
            "/api/v1/coleta/reader-captures",
            headers=system.capture_headers,
            json=system.capture_body,
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"] == expected
        assert response.headers["Cache-Control"] == "no-store"
    response = await system.http.get("/api/v1/financeiro/nativo/resumo", headers=system.headers())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "money_contract": 2,
        "totals_by_currency": [
            {
                "currency": "USDT",
                "bets": 1,
                "stake": "12.34",
                "returned": "30.85",
                "profit": "18.51",
            }
        ],
        "combined_monetary_total": None,
    }
    own_totals = response.json()
    filtered = await system.http.get(
        "/api/v1/financeiro/nativo/resumo",
        params={"account_id": system.account_id},
        headers=system.headers(),
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json() == own_totals
    unknown = await system.http.get(
        "/api/v1/financeiro/nativo/resumo",
        params={"account_id": 2**63 - 1},
        headers=system.headers(),
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["totals_by_currency"] == []
    invalid = await system.http.get(
        "/api/v1/financeiro/nativo/resumo",
        params={"account_id": "1.5"},
        headers=system.headers(),
    )
    assert invalid.status_code == 422
    response = await system.http.get("/api/v1/financeiro/nativo/apostas", headers=system.headers())
    assert response.json()[0]["stake"] == "12.34"
    foreign = await system.http.get(
        "/api/v1/financeiro/nativo/resumo",
        params={"account_id": system.account_id},
        headers=system.headers(system.other),
    )
    assert foreign.status_code == 200, foreign.text
    assert foreign.json()["totals_by_currency"] == []


async def test_native_http_accounts_require_jwt_and_owner_is_derived(native_http):
    system = native_http
    body = {
        "casa_id": system.house_id,
        "currency": "USDT",
        "label": "HTTP synthetic",
        "valid_from": "2027-01-01T00:00:00Z",
    }
    response = await system.http.post("/api/v1/financeiro/nativo/contas", json=body)
    assert response.status_code == 401
    response = await system.http.post(
        "/api/v1/financeiro/nativo/contas", headers=system.headers(system.other), json=body
    )
    assert response.status_code == 201, response.text
    foreign_id = response.json()["id"]
    response = await system.http.get("/api/v1/financeiro/nativo/contas", headers=system.headers())
    assert foreign_id not in {row["id"] for row in response.json()}


async def test_native_http_credential_revocation(native_http, como):
    from bancaemdia.models import ColetaInstalacao

    system = native_http
    missing = await system.http.post("/api/v1/coleta/reader-captures", json=system.capture_body)
    assert missing.status_code == 403
    malformed = await system.http.post(
        "/api/v1/coleta/reader-captures", json={"unknown": "synthetic"}
    )
    assert malformed.status_code == 403
    async with como(system.engine, system.user) as session:
        await session.execute(
            update(ColetaInstalacao)
            .where(ColetaInstalacao.id == system.credential["instalacao_id"])
            .values(revogado_em=datetime.now(UTC))
        )
        await session.commit()
    response = await system.http.post(
        "/api/v1/coleta/reader-captures", headers=system.capture_headers, json=system.capture_body
    )
    assert response.status_code == 403


async def test_native_http_flag_cannot_bypass_human_admission(native_http, monkeypatch):
    from bancaemdia.coleta.readers import native_admission

    monkeypatch.setattr(native_admission, "REVIEWED_BUNDLE_SHA256", None)
    response = await native_http.http.post(
        "/api/v1/coleta/reader-captures",
        headers=native_http.capture_headers,
        json=native_http.capture_body,
    )
    assert response.status_code == 503


async def test_native_http_validation_does_not_echo_private_values(native_http):
    body = {**native_http.capture_body, "private_marker": "private@example.invalid"}
    response = await native_http.http.post(
        "/api/v1/coleta/reader-captures", headers=native_http.capture_headers, json=body
    )
    assert response.status_code == 422
    assert "private@example.invalid" not in response.text
    body = json.dumps(native_http.capture_body).replace(
        '"reader-capture-1"', '"reader-capture-1", "transport_contract": "reader-capture-1"', 1
    )
    response = await native_http.http.post(
        "/api/v1/coleta/reader-captures",
        headers={**native_http.capture_headers, "Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == 422


async def test_native_http_daily_quota_is_shared_with_existing_collection(native_http, monkeypatch):
    from bancaemdia.config import get_settings
    from tests.integration.coleta.test_pairing import batch

    monkeypatch.setattr(get_settings(), "COLETA_DAILY_LIMIT", 1)
    first = await native_http.http.post(
        "/api/v1/coleta/reader-captures",
        headers=native_http.capture_headers,
        json=native_http.capture_body,
    )
    assert first.status_code == 200, first.text
    for path, body in (
        ("/api/v1/coleta", batch()),
        ("/api/v1/coleta/reader-captures", native_http.capture_body),
    ):
        response = await native_http.http.post(path, headers=native_http.capture_headers, json=body)
        assert response.status_code == 429, response.text


async def test_native_http_foreign_multicontas_is_rejected(native_http, como):
    _, aid = await prepare(native_http.engine, como, native_http.other)
    response = await native_http.http.post(
        "/api/v1/coleta/reader-captures",
        headers=native_http.capture_headers,
        json={**native_http.capture_body, "multicontas": True, "explicit_account_id": aid},
    )
    assert response.status_code == 422


async def test_native_http_export_and_erasure_preserve_precision_and_privacy(native_http):
    system = native_http
    text_value = json.dumps(native_source()).replace("30.85", "30.850000000000000001")
    source = captured(text=text_value)[0].model_dump(mode="json")
    response = await system.http.post(
        "/api/v1/coleta/reader-captures",
        headers=system.capture_headers,
        json={**system.capture_body, "envelope": source},
    )
    assert response.status_code == 200, response.text
    exported = await system.http.get("/api/v1/usuario/me/export", headers=system.headers())
    assert exported.status_code == 200, exported.text
    assert exported.json()["native_bets"][0]["returned"] == "30.850000000000000001000000000000"
    erased = await system.http.delete("/api/v1/usuario/me", headers=system.headers())
    assert erased.status_code == 200, erased.text
    async with system.admin.begin() as connection:
        for model in (NativeBetEvidence, NativeBet, NativeAccount):
            assert (
                await connection.scalar(
                    select(func.count()).select_from(model).where(model.usuario_id == system.user)
                )
                == 0
            )


async def test_native_admission_count_uses_receipt_day_not_capture_day(
    engine_app, como, novo_usuario
):
    from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo

    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured(captured_at="2020-01-01T00:00:00Z")
    async with como(engine_app, uid) as session:
        await materialize_native(session, uid, hid, bet, value)
        assert (
            await ColetaCasaRepo().count_received_since(
                session, uid, datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            )
            == 1
        )


async def test_native_db_blocks_inactive_owner_writes(engine_admin, engine_app, como, novo_usuario):
    from sqlalchemy.exc import DBAPIError

    from bancaemdia.models import Usuario

    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    async with engine_admin.begin() as connection:
        await connection.execute(update(Usuario).where(Usuario.id == uid).values(ativo=False))
    async with como(engine_app, uid) as session:
        with pytest.raises(DBAPIError):
            await account(session, uid, hid)


async def test_exact_money_materializes_and_duplicate_is_noop(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, aid = await prepare(engine_app, como, uid)
    value, bet = captured()
    async with como(engine_app, uid) as session:
        first = await materialize_native(session, uid, hid, bet, value)
        await session.commit()
    async with como(engine_app, uid) as session:
        again = await materialize_native(session, uid, hid, bet, value)
        assert again.bet_id == first.bet_id and again.result == "noop"
        row = await session.get(NativeBet, first.bet_id)
        assert row.account_id == aid and row.currency == "USDT"
        assert row.stake == Decimal("12.34") and row.returned == Decimal("30.85")
        assert await session.scalar(select(func.count()).select_from(Aposta)) == 0
        totals = await native_summary(session, uid)
        assert totals == [
            {
                "currency": "USDT",
                "bets": 1,
                "stake": "12.34",
                "returned": "30.85",
                "profit": "18.51",
            }
        ]


async def test_game_day_selects_account_instead_of_placement(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    async with como(engine_app, uid) as session:
        hid = await house(session)
        await account(session, uid, hid, valid_to=datetime(2026, 1, 3, tzinfo=UTC))
        selected = await account(session, uid, hid, valid_from=datetime(2026, 1, 3, tzinfo=UTC))
        value, bet = captured()
        result = await materialize_native(session, uid, hid, bet, value)
        row = await session.get(NativeBet, result.bet_id)
        assert row.account_id == selected.id


async def test_only_multicontas_can_override_game_account(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, aid = await prepare(engine_app, como, uid, valid_to=datetime(2026, 1, 3, tzinfo=UTC))
    value, bet = captured()
    async with como(engine_app, uid) as session:
        with pytest.raises(ValueError, match="multicontas"):
            await materialize_native(session, uid, hid, bet, value, explicit_account_id=aid)
        result = await materialize_native(
            session, uid, hid, bet, value, explicit_account_id=aid, multicontas=True
        )
        row = await session.get(NativeBet, result.bet_id)
        assert row.account_id == aid and not row.needs_review


async def test_absent_account_retains_fact_outside_totals(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    async with como(engine_app, uid) as session:
        hid = await house(session)
        value, bet = captured()
        outcome = await materialize_native(session, uid, hid, bet, value)
        assert outcome.result == "account_review" and outcome.needs_review
        assert await native_summary(session, uid) == []


async def test_ambiguous_account_never_picks_smallest_id(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    async with como(engine_app, uid) as session:
        await account(session, uid, hid)
        value, bet = captured()
        outcome = await materialize_native(session, uid, hid, bet, value)
        assert outcome.needs_review
        assert (await session.get(NativeBet, outcome.bet_id)).account_id is None


async def test_usdt_cannot_bind_a_brl_account(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, aid = await prepare(engine_app, como, uid, currency="BRL")
    value, bet = captured()
    async with como(engine_app, uid) as session:
        with pytest.raises(ValueError, match="currency"):
            await materialize_native(
                session, uid, hid, bet, value, explicit_account_id=aid, multicontas=True
            )
        outcome = await materialize_native(session, uid, hid, bet, value)
        assert outcome.needs_review


async def test_foreign_account_reference_is_rejected(engine_app, como, novo_usuario):
    alice, bob = await novo_usuario(), await novo_usuario()
    hid, aid = await prepare(engine_app, como, alice)
    value, bet = captured()
    async with como(engine_app, bob) as session:
        with pytest.raises(ValueError, match="owned account"):
            await materialize_native(
                session, bob, hid, bet, value, explicit_account_id=aid, multicontas=True
            )


async def test_unversioned_content_change_excludes_identity_and_preserves_both_facts(
    engine_app, como, novo_usuario
):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()
    async with como(engine_app, uid) as session:
        first = await materialize_native(session, uid, hid, bet, value)
        await session.commit()
    changed_value, changed_bet = captured(
        native_source(profitAmount=31.73), captured_at="2020-01-01T00:00:00Z"
    )
    async with como(engine_app, uid) as session:
        changed = await materialize_native(session, uid, hid, changed_bet, changed_value)
        assert changed.result == "conflict_review"
        row = await session.get(NativeBet, first.bet_id)
        assert row.returned == Decimal("30.85") and row.needs_review
        assert await native_summary(session, uid) == []
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NativeBetEvidence)
                .where(NativeBetEvidence.usuario_id == uid)
            )
            == 2
        )


async def test_replay_does_not_silently_clear_existing_conflict(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()
    async with como(engine_app, uid) as session:
        await materialize_native(session, uid, hid, bet, value)
        changed_value, changed_bet = captured(native_source(profitAmount=31.73))
        await materialize_native(session, uid, hid, changed_bet, changed_value)
        replay = await materialize_native(session, uid, hid, bet, value)
        assert replay.needs_review and await native_summary(session, uid) == []


async def test_equal_native_value_with_different_bytes_keeps_each_source_hash(
    engine_app, como, novo_usuario
):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()
    altered = json.dumps(native_source(), indent=2)
    other_value, other_bet = captured(text=altered)
    async with como(engine_app, uid) as session:
        await materialize_native(session, uid, hid, bet, value)
        result = await materialize_native(session, uid, hid, other_bet, other_value)
        assert result.result == "noop" and not result.needs_review
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NativeBetEvidence)
                .where(NativeBetEvidence.usuario_id == uid)
            )
            == 2
        )


async def test_concurrent_duplicate_native_capture_converges(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()

    async def ingest():
        async with como(engine_app, uid) as session:
            result = await materialize_native(session, uid, hid, bet, value)
            await session.commit()
            return result.bet_id

    assert len(set(await asyncio.gather(*(ingest() for _ in range(5))))) == 1
    async with como(engine_app, uid) as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(NativeBet).where(NativeBet.usuario_id == uid)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NativeBetEvidence)
                .where(NativeBetEvidence.usuario_id == uid)
            )
            == 1
        )


async def test_evidence_cannot_be_changed_or_deleted_by_application_role(
    engine_app, como, novo_usuario
):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()
    async with como(engine_app, uid) as session:
        await materialize_native(session, uid, hid, bet, value)
        await session.commit()
    async with como(engine_app, uid) as session:
        changed = await session.execute(
            update(NativeBetEvidence)
            .where(NativeBetEvidence.usuario_id == uid)
            .values(source_hash="0" * 64)
        )
        assert changed.rowcount == 0
        removed = await session.execute(
            text("DELETE FROM native_bet_evidence WHERE usuario_id=:uid"), {"uid": uid}
        )
        assert removed.rowcount == 0


@pytest.mark.parametrize("scope", ["foreign", "none"])
async def test_native_accounts_and_money_are_hidden_by_rls(engine_app, como, novo_usuario, scope):
    alice, bob = await novo_usuario(), await novo_usuario()
    hid, aid = await prepare(engine_app, como, alice)
    value, bet = captured()
    async with como(engine_app, alice) as session:
        first = await materialize_native(session, alice, hid, bet, value)
        await session.commit()
    async with como(engine_app, bob if scope == "foreign" else None) as session:
        assert await session.get(NativeAccount, aid) is None
        assert await session.get(NativeBet, first.bet_id) is None
        assert await session.scalar(select(func.count()).select_from(NativeBetEvidence)) == 0
        with pytest.raises(PermissionError):
            await native_summary(session, alice)


async def test_canonical_money_cannot_be_forged_away_from_source(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()
    forged = bet.model_copy(update={"returned": Decimal("999")})
    async with como(engine_app, uid) as session:
        with pytest.raises(ValueError, match="does not match source"):
            await materialize_native(session, uid, hid, forged, value)


async def test_postgres_does_not_round_a_thirty_decimal_source_return(
    engine_app, como, novo_usuario
):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    exact = "30.850000000000000000000000000001"
    value, bet = captured(text=json.dumps(native_source()).replace("30.85", exact))
    async with como(engine_app, uid) as session:
        result = await materialize_native(session, uid, hid, bet, value)
        await session.commit()
    async with como(engine_app, uid) as session:
        row = await session.get(NativeBet, result.bet_id)
        assert row.returned == Decimal(exact)
        assert (await native_summary(session, uid))[0][
            "profit"
        ] == "18.510000000000000000000000000001"


async def test_native_report_date_filter_uses_game_instead_of_placement(
    engine_app, como, novo_usuario
):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()
    async with como(engine_app, uid) as session:
        await materialize_native(session, uid, hid, bet, value)
        assert await native_summary(session, uid, until=datetime(2026, 1, 3, tzinfo=UTC)) == []
        assert len(await native_summary(session, uid, since=datetime(2026, 1, 3, tzinfo=UTC))) == 1


async def test_reports_keep_currencies_separate(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    hid, _ = await prepare(engine_app, como, uid)
    value, bet = captured()
    async with como(engine_app, uid) as session:
        await materialize_native(session, uid, hid, bet, value)
        brl = await account(session, uid, hid, currency="BRL")
        session.add(
            NativeBet(
                usuario_id=uid,
                account_id=brl.id,
                currency="BRL",
                identity_hash="b" * 64,
                canonical_hash="c" * 64,
                source_hash="d" * 64,
                state="RED",
                stake=Decimal("7.35"),
                returned=Decimal(0),
                game_at=bet.game_at,
                placed_at=bet.placed_at,
                needs_review=False,
                canonical={"evidence": "transient-storage-test"},
            )
        )
        await session.flush()
        rows = await native_summary(session, uid)
        assert [row["currency"] for row in rows] == ["BRL", "USDT"]
        assert rows[0]["profit"] == "-7.35" and rows[1]["profit"] == "18.51"


async def test_native_http_drift_is_quarantined_once_without_money(native_http, como):
    from bancaemdia.models import ReaderQuarantine

    raw = native_source()
    del raw["selections"][0]["match"]["startAt"]
    value = ReaderEnvelope.model_validate(envelope(raw)).model_dump(mode="json")
    for _ in range(2):
        response = await native_http.http.post(
            "/api/v1/coleta/reader-captures",
            headers=native_http.capture_headers,
            json={**native_http.capture_body, "envelope": value},
        )
        assert response.status_code == 422
    async with como(native_http.engine, native_http.user) as session:
        assert await session.scalar(select(func.count()).select_from(ReaderQuarantine)) == 1
        assert await session.scalar(select(func.count()).select_from(NativeBet)) == 0


async def test_native_http_unsafe_source_retains_only_fingerprint(native_http, como):
    from bancaemdia.models import ReaderQuarantine

    raw = native_source()
    raw["email"] = "private@example.invalid"
    value = ReaderEnvelope.model_validate(envelope(raw)).model_dump(mode="json")
    response = await native_http.http.post(
        "/api/v1/coleta/reader-captures",
        headers=native_http.capture_headers,
        json={**native_http.capture_body, "envelope": value},
    )
    assert response.status_code == 422 and "private@example.invalid" not in response.text
    async with como(native_http.engine, native_http.user) as session:
        row = await session.scalar(select(ReaderQuarantine))
        assert row.envelope is None and row.error_code == "unsafe_payload"


async def test_native_http_denies_read_only_collection_and_account_write(native_http, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from bancaemdia.db.session import get_db
    from bancaemdia.main import app

    # Activate billing only within this rolled-back transaction. HTTP uses the
    # real restricted role and sees the same PostgreSQL access decision.
    async with native_http.admin.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SELECT billing_activate_rollout()"))
            await connection.execute(text("SET LOCAL ROLE bancaemdia_app"))

            async def sessions():
                async with AsyncSession(
                    bind=connection, join_transaction_mode="create_savepoint"
                ) as session:
                    yield session

            monkeypatch.setitem(app.dependency_overrides, get_db, sessions)
            capture = await native_http.http.post(
                "/api/v1/coleta/reader-captures",
                headers=native_http.capture_headers,
                json=native_http.capture_body,
            )
            assert capture.status_code == 402, capture.text
            read = await native_http.http.get(
                "/api/v1/financeiro/nativo/resumo", headers=native_http.headers()
            )
            assert read.status_code == 200, read.text
            write = await native_http.http.post(
                "/api/v1/financeiro/nativo/contas",
                headers=native_http.headers(),
                json={
                    "casa_id": native_http.house_id,
                    "currency": "USDT",
                    "label": "Synthetic",
                    "valid_from": "2027-01-01T00:00:00Z",
                },
            )
            assert write.status_code == 402, write.text
        finally:
            await transaction.rollback()


async def test_native_populated_migration_refuses_destructive_downgrade(
    engine_app, como, novo_usuario, banco
):
    from alembic import command
    from alembic.config import Config

    from tests.conftest import ROOT, _em_outra_thread

    uid = await novo_usuario()
    await prepare(engine_app, como, uid)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = banco.url_admin
    try:
        with pytest.raises(Exception, match="native financial evidence exists"):
            await asyncio.to_thread(
                _em_outra_thread, lambda: command.downgrade(config, "j6main2026")
            )
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous
    async with como(engine_app, uid) as session:
        assert await session.scalar(select(func.count()).select_from(NativeAccount)) == 1
