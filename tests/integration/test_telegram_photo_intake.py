"""Linked Telegram photos become one tenant-owned draft and durable replies."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from bancaemdia import models
from bancaemdia.integrations.telegram.codec import decrypt_payload
from bancaemdia.integrations.telegram.webhook import ingest_update
from bancaemdia.repositories.rascunho_aposta import RascunhoApostaRepo
from bancaemdia.services.telegram_conversation import handle_text
from bancaemdia.workers import telegram_extraction
from bancaemdia.workers.telegram import process_inbox_once


def _id() -> int:
    return uuid4().int & ((1 << 62) - 1)


async def _linked(engine_app: AsyncEngine, como, novo_usuario) -> tuple[int, int]:
    user = await novo_usuario()
    chat = 8_000_000_000 + uuid4().int % 1_000_000
    async with como(engine_app, user) as session:
        session.add(
            models.TelegramLink(usuario_id=user, telegram_user_id=chat, telegram_chat_id=chat)
        )
        await session.commit()
    return user, chat


async def _ingest(engine_app: AsyncEngine, como, chat: int, update_id: int, message: dict) -> None:
    async with como(engine_app, None) as session:
        assert await ingest_update(
            session,
            {
                "update_id": update_id,
                "message": {
                    "message_id": 77,
                    "from": {"id": chat},
                    "chat": {"id": chat, "type": "private"},
                    **message,
                },
            },
        )
        await session.commit()
    for _ in range(30):
        assert await process_inbox_once(engine_app)
        async with como(engine_app, None) as session:
            await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
            inbox = await session.scalar(
                select(models.TelegramInbox).where(models.TelegramInbox.update_id == update_id)
            )
            if inbox is not None and inbox.status == "DONE":
                return
    pytest.fail("Telegram update not processed")


@pytest.mark.asyncio
async def test_forwarded_photo_extraction_is_deduplicated_and_never_materializes(
    engine_app: AsyncEngine, como, novo_usuario, monkeypatch
) -> None:
    user, chat = await _linked(engine_app, como, novo_usuario)
    update_id = _id()
    message = {
        "photo": [
            {"file_id": "small", "file_unique_id": "small-unique", "file_size": 100},
            {"file_id": "large", "file_unique_id": "large-unique", "file_size": 300},
        ],
        "forward_origin": {"type": "channel", "date": 1_760_000_000, "chat": {"title": "private"}},
    }
    await _ingest(engine_app, como, chat, update_id, message)
    async with como(engine_app, user) as session:
        draft = await RascunhoApostaRepo().active(session, user, chat)
        assert draft is not None and draft.status == "AWAITING_EXTRACTION"
        assert decrypt_payload(draft.media_reference_ciphertext)["file_id"] == "large"
        assert draft.source_metadata_json["file_unique_id"] == "large-unique"
        assert draft.source_metadata_json["forwarded"] is True
        assert draft.source_metadata_json["forward_origin_type"] == "channel"
        assert "private" not in str(draft.source_metadata_json)
        draft_id = draft.id
    async with como(engine_app, user) as session:
        corrected = await handle_text(
            session, user_id=user, chat_id=chat, update_id=_id(), text="/corrigir odd 1,95"
        )
        assert corrected is not None and corrected.draft is not None
        assert corrected.draft.status == "AWAITING_INFORMATION"
        await session.commit()
    async with como(engine_app, None) as session:
        assert not await ingest_update(session, {"update_id": update_id, "message": message})

    async def fake_read(claim, client):
        await asyncio.sleep(0)
        assert claim.id == draft_id
        return (
            b"\xff\xd8\xff\xe0slip",
            "image/jpeg",
            {
                "bilhete": {
                    "casa": "Betano",
                    "odd_total": 1.9,
                    "evento": "Jogo",
                    "quando": "2026-09-20T21:00:00-03:00",
                    "confianca": 0.95,
                }
            },
        )

    monkeypatch.setattr(telegram_extraction, "read_photo", fake_read)
    assert await telegram_extraction.process_photo_once(engine_app, object(), draft_id=draft_id)
    assert not await telegram_extraction.process_photo_once(engine_app, object(), draft_id=draft_id)
    async with como(engine_app, user) as session:
        draft = await session.get(models.RascunhoAposta, draft_id)
        assert draft is not None and draft.status == "AWAITING_INFORMATION"
        assert draft.media_hash == draft.source_metadata_json["content_hash"]
        assert draft.fields_json["odd"] == pytest.approx(1.95)
        assert draft.field_meta_json["odd"] == {"source": "user", "confidence": 1.0}
        assert draft.field_meta_json["casa"] == {"source": "extraction", "confidence": 0.95}
        assert "stake_unidades" in draft.missing_fields_json
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == user)
            )
            == 0
        )
        reply = await session.scalar(
            select(models.TelegramOutbox).where(
                models.TelegramOutbox.usuario_id == user,
                models.TelegramOutbox.idempotency_key == f"telegram-photo:{draft_id}:extraction",
            )
        )
        assert reply is not None and "stake" in decrypt_payload(reply.payload_ciphertext)["text"]


@pytest.mark.asyncio
async def test_multiple_coupons_wait_for_one_choice_and_unreadable_asks_fields(
    engine_app: AsyncEngine, como, novo_usuario, monkeypatch
) -> None:
    user, chat = await _linked(engine_app, como, novo_usuario)
    await _ingest(engine_app, como, chat, _id(), {"photo": [{"file_id": "one"}]})
    async with como(engine_app, user) as session:
        draft = await RascunhoApostaRepo().active(session, user, chat)
        assert draft is not None
        draft_id = draft.id

    async def fake_read(claim, client):
        await asyncio.sleep(0)
        return (
            b"\xff\xd8\xff\xe0slip",
            "image/jpeg",
            {
                "cupons": [
                    {"bilhete": {"casa": "Betano", "odd_total": 1.8, "confianca": 0.95}},
                    {"bilhete": {"casa": "Bet365", "odd_total": 2.1, "confianca": 0.95}},
                ]
            },
        )

    monkeypatch.setattr(telegram_extraction, "read_photo", fake_read)
    assert await telegram_extraction.process_photo_once(engine_app, object(), draft_id=draft_id)
    async with como(engine_app, user) as session:
        pending = await session.get(models.RascunhoAposta, draft_id)
        assert pending is not None and pending.status == "AWAITING_INFORMATION"
        assert pending.fields_json == {} and len(pending.coupon_candidates_json) == 2
        response = await handle_text(
            session, user_id=user, chat_id=chat, update_id=_id(), text="cupom=2"
        )
        assert response is not None and response.draft is not None
        assert response.draft.fields_json["odd"] == pytest.approx(2.1)
        assert response.draft.coupon_candidates_json == []
        await session.commit()
    async with como(engine_app, user) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == user)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_album_and_text_only_get_supported_flow_without_draft(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user, chat = await _linked(engine_app, como, novo_usuario)
    album_update = _id()
    await _ingest(
        engine_app,
        como,
        chat,
        album_update,
        {"photo": [{"file_id": "one"}], "media_group_id": "album-1"},
    )
    text_update = _id()
    await _ingest(engine_app, como, chat, text_update, {"text": "odd=2,10; stake=2"})
    async with como(engine_app, user) as session:
        assert await RascunhoApostaRepo().active(session, user, chat) is None
        replies = (
            await session.scalars(
                select(models.TelegramOutbox).where(
                    models.TelegramOutbox.usuario_id == user,
                    models.TelegramOutbox.idempotency_key.in_([
                        f"telegram-draft:{album_update}:reply",
                        f"telegram-draft:{text_update}:reply",
                    ]),
                )
            )
        ).all()
        assert len(replies) == 2
        assert all("foto" in decrypt_payload(item.payload_ciphertext)["text"] for item in replies)


@pytest.mark.asyncio
async def test_unreadable_photo_asks_for_values_without_second_upload(
    engine_app: AsyncEngine, como, novo_usuario, monkeypatch
) -> None:
    user, chat = await _linked(engine_app, como, novo_usuario)
    await _ingest(engine_app, como, chat, _id(), {"photo": [{"file_id": "unreadable"}]})
    async with como(engine_app, user) as session:
        draft = await RascunhoApostaRepo().active(session, user, chat)
        assert draft is not None
        draft_id = draft.id

    async def fake_read(claim, client):
        await asyncio.sleep(0)
        return b"\xff\xd8\xff\xe0slip", "image/jpeg", {"bilhete": {"ilegivel": True}}

    monkeypatch.setattr(telegram_extraction, "read_photo", fake_read)
    assert await telegram_extraction.process_photo_once(engine_app, object(), draft_id=draft_id)
    async with como(engine_app, user) as session:
        draft = await session.get(models.RascunhoAposta, draft_id)
        assert draft is not None and draft.status == "AWAITING_INFORMATION"
        assert {"casa", "odd", "stake_unidades", "data_aposta"}.issubset(draft.missing_fields_json)
        response = await handle_text(
            session, user_id=user, chat_id=chat, update_id=_id(), text="/continuar"
        )
        assert response is not None and "foto já está guardada" in response.text
