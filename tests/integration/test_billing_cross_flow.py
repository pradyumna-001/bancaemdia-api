"""Billing-only acceptance across the existing holder and Telegram PRs."""

import importlib
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

if os.environ.get("BILLING_CROSS_FLOW_REQUIRED") == "1":
    importlib.import_module("bancaemdia.api.v1.telegram")
else:
    pytest.importorskip(
        "bancaemdia.api.v1.telegram",
        reason="Requires #141; mandatory Billing cross-flow CI assembles and runs these scenarios",
    )
from fastapi import FastAPI, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from bancaemdia import models
from bancaemdia.api.v1 import telegram as telegram_api
from bancaemdia.api.v1 import titulares, usuario
from bancaemdia.db.session import get_db, get_db_primary, get_db_primary_snapshot, get_db_snapshot
from bancaemdia.domain.access import AccountReadOnlyError
from bancaemdia.integrations.telegram.codec import decrypt_payload
from bancaemdia.integrations.telegram.webhook import ingest_update
from bancaemdia.main import account_read_only_error, billing_database_error
from bancaemdia.services.telegram_conversation import open_draft
from bancaemdia.workers import telegram_extraction
from bancaemdia.workers.telegram import BILLING_DENIAL, process_inbox_once

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


@pytest.fixture
async def billing_control(engine_admin):
    async def enable(uid, *, active=False):
        async with engine_admin.begin() as conn:
            await conn.execute(text("SELECT billing_activate_rollout()"))
            price = await conn.scalar(
                text("""
                INSERT INTO billing_prices(amount_cents,currency,frequency,valid_from)
                VALUES (12345,'BRL','MONTHLY',now()) RETURNING id
            """)
            )
            await conn.execute(
                text("""
                UPDATE assinaturas SET trial_confirmed=true,
                    trial_started_at=now()-interval '8 days',
                    trial_ends_at=now()-interval '1 day',
                    status=:status, price_id=:price,
                    current_period_started_at=now()-interval '1 hour',
                    current_period_ends_at=now()+interval '1 day'
                WHERE usuario_id=:uid
            """),
                {"uid": uid, "status": "ACTIVE" if active else "EXPIRED", "price": price},
            )

    yield enable
    async with engine_admin.begin() as conn:
        await conn.execute(text("UPDATE billing_rollout SET activated_at=NULL WHERE id=1"))


async def linked(engine_app, como, novo_usuario):
    uid = await novo_usuario()
    chat = uuid4().int % 2**50
    async with como(engine_app, uid) as session:
        session.add(
            models.TelegramLink(usuario_id=uid, telegram_user_id=chat, telegram_chat_id=chat)
        )
        await session.commit()
    return uid, chat


async def ingest(engine_app, como, chat, body, update_id=None):
    update_id = update_id or uuid4().int % 2**50
    async with como(engine_app, None) as session:
        await ingest_update(
            session,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "from": {"id": chat},
                    "chat": {"id": chat, "type": "private"},
                    **body,
                },
            },
        )
        await session.commit()
    # Drain any older fixture messages too; never mistake their ACK for this one.
    for _ in range(100):
        await process_inbox_once(engine_app)
        async with como(engine_app, None) as session:
            await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
            item = await session.scalar(
                select(models.TelegramInbox).where(models.TelegramInbox.update_id == update_id)
            )
            if item.status == "DONE":
                assert item.attempts == 0
                assert item.payload_ciphertext is None
                return update_id
    pytest.fail("denial was retried or not acknowledged")


async def draft(engine_app, como, uid, chat):
    async with como(engine_app, uid) as session:
        row, created = await open_draft(
            session,
            user_id=uid,
            chat_id=chat,
            message_id=uuid4().int % 2**50,
            update_id=uuid4().int % 2**50,
            media_file_id="fixture-photo",
            media_reference={"file_id": "fixture-photo"},
        )
        assert created
        await session.commit()
        return row.id


async def replies(engine_app, como, uid):
    async with como(engine_app, uid) as session:
        rows = (
            await session.scalars(
                select(models.TelegramOutbox).where(models.TelegramOutbox.usuario_id == uid)
            )
        ).all()
        return [decrypt_payload(row.payload_ciphertext)["text"] for row in rows]


async def test_expired_telegram_denies_photo_edit_confirmation_without_retry(
    engine_app,
    como,
    novo_usuario,
    billing_control,
    monkeypatch,
):
    uid, chat = await linked(engine_app, como, novo_usuario)
    await billing_control(uid)

    def forbidden(*args, **kwargs):
        pytest.fail("denied ingress reached photo processing")

    monkeypatch.setattr(telegram_extraction, "read_photo", forbidden)
    photo = {"photo": [{"file_id": "fixture", "file_unique_id": "fixture", "file_size": 5}]}
    update = await ingest(engine_app, como, chat, photo)
    await ingest(engine_app, como, chat, photo, update)
    await ingest(engine_app, como, chat, {"text": "stake=10"})
    await ingest(engine_app, como, chat, {"text": "/confirmar"})
    assert await replies(engine_app, como, uid) == [BILLING_DENIAL] * 3
    async with como(engine_app, uid) as session:
        for cls in (models.RascunhoAposta, models.Aposta, models.Evento):
            assert (
                await session.scalar(
                    select(func.count()).select_from(cls).where(cls.usuario_id == uid)
                )
                == 0
            )


async def test_expired_draft_stays_readable_cancellable_and_sql_cannot_change_content(
    engine_app,
    como,
    novo_usuario,
    billing_control,
):
    uid, chat = await linked(engine_app, como, novo_usuario)
    did = await draft(engine_app, como, uid, chat)
    await billing_control(uid)
    await ingest(engine_app, como, chat, {"text": "/continuar"})
    async with como(engine_app, uid) as session:
        for statement in (
            "UPDATE rascunhos_aposta SET fields_json='{\"odd\": 2}' WHERE id=:id",
            "UPDATE rascunhos_aposta SET status='CANCELLED',version=version+1,closed_at=now(),fields_json='{\"odd\": 2}' WHERE id=:id",
            "UPDATE rascunhos_aposta SET extraction_error_code='account_read_only',fields_json='{\"odd\": 2}' WHERE id=:id",
        ):
            savepoint = await session.begin_nested()
            with pytest.raises(DBAPIError) as exc:
                await session.execute(text(statement), {"id": did})
            assert exc.value.orig.sqlstate == "P0402"
            await savepoint.rollback()
    await ingest(engine_app, como, chat, {"text": "/cancelar"})
    async with como(engine_app, uid) as session:
        row = await session.get(models.RascunhoAposta, did)
        assert row.status == "CANCELLED" and row.fields_json == {}
    assert len(await replies(engine_app, como, uid)) == 2


@pytest.mark.parametrize("during_read", [False, True])
async def test_queued_photo_expiry_preserves_draft_and_never_materializes(
    engine_admin,
    engine_app,
    como,
    novo_usuario,
    billing_control,
    monkeypatch,
    during_read,
):
    uid, chat = await linked(engine_app, como, novo_usuario)
    did = await draft(engine_app, como, uid, chat)
    await billing_control(uid, active=during_read)
    calls = []

    async def read(claim, client):
        calls.append(claim.id)
        assert during_read
        async with engine_admin.begin() as conn:
            await conn.execute(
                text("UPDATE assinaturas SET status='EXPIRED' WHERE usuario_id=:uid"), {"uid": uid}
            )
        return b"synthetic fixture", "image/png", {}

    monkeypatch.setattr(telegram_extraction, "read_photo", read)
    await telegram_extraction.process_photo_once(engine_app, object(), draft_id=did)
    assert len(calls) == int(during_read)
    async with como(engine_app, uid) as session:
        row = await session.get(models.RascunhoAposta, did)
        assert row.status == "AWAITING_EXTRACTION" and row.fields_json == {}
        assert row.extraction_completed_at is None and row.media_hash is None
        assert row.media_reference_ciphertext is not None
        assert row.extraction_error_code == "account_read_only"
        assert row.extraction_lease_token is None
        assert row.extraction_attempts == int(during_read)
        assert row.extraction_next_attempt_at > datetime.now(UTC)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == uid)
            )
            == 0
        )
    assert await replies(engine_app, como, uid) == [BILLING_DENIAL]

    if not during_read:
        async with engine_admin.begin() as conn:
            await conn.execute(
                text("UPDATE assinaturas SET status='ACTIVE' WHERE usuario_id=:uid"), {"uid": uid}
            )
            await conn.execute(
                text("UPDATE rascunhos_aposta SET extraction_next_attempt_at=now() WHERE id=:id"),
                {"id": did},
            )

        monkeypatch.setattr(
            telegram_extraction,
            "read_photo",
            AsyncMock(return_value=(b"synthetic fixture", "image/png", {})),
        )
        assert await telegram_extraction.process_photo_once(engine_app, object(), draft_id=did)
        assert not await telegram_extraction.process_photo_once(engine_app, object(), draft_id=did)
        async with como(engine_app, uid) as session:
            row = await session.get(models.RascunhoAposta, did)
            assert row.extraction_completed_at is not None and row.extraction_attempts == 1
            assert row.status == "AWAITING_INFORMATION"


async def test_expired_holder_api_reads_financials_export_and_revocation_survive(
    engine_app,
    como,
    novo_usuario,
    billing_control,
):
    uid, _chat = await linked(engine_app, como, novo_usuario)
    other = await novo_usuario()
    async with como(engine_app, uid) as session:
        holder = models.Titular(usuario_id=uid, nome="Owned fixture")
        session.add(holder)
        await session.commit()
        hid = holder.id
    await billing_control(uid)
    app = FastAPI()
    app.include_router(titulares.router)
    app.include_router(usuario.router)
    app.include_router(telegram_api.router)
    app.add_exception_handler(AccountReadOnlyError, account_read_only_error)
    app.add_exception_handler(DBAPIError, billing_database_error)

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.usuario_id = int(request.headers.get("X-Fixture-User", uid))
        return await call_next(request)

    async def db(request: Request):
        async with como(engine_app, request.state.usuario_id) as session:
            yield session

    for dependency in (get_db, get_db_primary, get_db_snapshot, get_db_primary_snapshot):
        app.dependency_overrides[dependency] = db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        for url in (
            "/api/v1/titulares",
            f"/api/v1/titulares/{hid}",
            f"/api/v1/titulares/{hid}/matriz",
            "/api/v1/titulares/financeiro",
            "/api/v1/usuario/me/export",
        ):
            response = await client.get(url)
            assert response.status_code == 200, (url, response.text)
        for method, url, data in (
            ("POST", "/api/v1/titulares", {"nome": "Denied"}),
            ("PATCH", f"/api/v1/titulares/{hid}", {"nome": "Denied"}),
            ("DELETE", f"/api/v1/titulares/{hid}", None),
        ):
            response = await client.request(method, url, json=data)
            assert response.status_code == 402 and response.json() == {
                "detail": "account_read_only"
            }
        assert (
            await client.get(f"/api/v1/titulares/{hid}", headers={"X-Fixture-User": str(other)})
        ).status_code == 404
        assert (await client.delete("/api/v1/telegram/link")).status_code == 200
