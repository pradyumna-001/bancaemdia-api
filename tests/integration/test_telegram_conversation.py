"""PostgreSQL acceptance checks for resumable, non-financial bet drafts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from bancaemdia import models
from bancaemdia.integrations.telegram.codec import decrypt_payload
from bancaemdia.integrations.telegram.webhook import ingest_update
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.rascunho_aposta import DraftVersionConflictError, RascunhoApostaRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo
from bancaemdia.services.telegram_conversation import (
    apply_extraction,
    handle_text,
    new_photo_reply,
    open_draft,
)
from bancaemdia.workers.telegram import process_inbox_once


async def _account(engine_admin: AsyncEngine, engine_app: AsyncEngine, como, user: int) -> int:
    async with engine_admin.begin() as conn:
        await conn.execute(insert(models.Casa).values(nome="Betano").on_conflict_do_nothing())
        house = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    assert house is not None
    async with como(engine_app, user) as session:
        account = await ContaCasaRepo().create(
            session,
            {"usuario_id": user, "casa_id": house, "apelido": "principal", "estado": "EM_USO"},
        )
        await UsoContaCasaRepo().open(
            session, user, house, account.id, datetime(2026, 1, 1, tzinfo=UTC)
        )
        await session.commit()
    return account.id


@pytest.mark.asyncio
async def test_missing_house_and_stake_resume_without_photo_or_financial_bet(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user, stranger = await novo_usuario(), await novo_usuario()
    account_id = await _account(engine_admin, engine_app, como, user)
    chat = 8_000_000 + uuid4().int % 1_000_000
    async with como(engine_app, user) as session:
        draft, created = await open_draft(
            session,
            user_id=user,
            chat_id=chat,
            message_id=10,
            update_id=101,
            media_file_id="private-photo-file-id",
            extracted={
                "odd": 1.82,
                "evento": "Corinthians x Santos",
                "data_aposta": "2026-09-20T21:00:00-03:00",
            },
            confidence={"odd": 0.96},
        )
        assert created and draft.status == "AWAITING_INFORMATION"
        assert draft.missing_fields_json == ["casa", "stake_unidades"]
        assert draft.media_reference_ciphertext is not None
        assert b"private-photo-file-id" not in draft.media_reference_ciphertext
        assert (
            decrypt_payload(draft.media_reference_ciphertext)["file_id"] == "private-photo-file-id"
        )
        identifier = draft.id
        await session.commit()

    # A fresh session stands in for a process restart; no media is supplied again.
    async with como(engine_app, user) as session:
        resumed = await handle_text(
            session, user_id=user, chat_id=chat, update_id=102, text="/continuar"
        )
        assert resumed is not None and resumed.draft is not None
        assert resumed.draft.id == identifier
        assert "Corinthians x Santos" in resumed.text
        assert "casa, stake em unidades" in resumed.text
        assert "conta da casa" not in resumed.text
        assert "foto" in resumed.text.lower()
        another = await new_photo_reply(
            session,
            user_id=user,
            chat_id=chat,
            message_id=11,
            update_id=103,
            media_file_id="different-file",
        )
        assert another.draft is not None and another.draft.id == identifier
        assert "/cancelar" in another.text
        await session.commit()

    async with como(engine_app, user) as session:
        corrected = await handle_text(
            session,
            user_id=user,
            chat_id=chat,
            update_id=104,
            text="casa=Betano; stake=2",
        )
        assert corrected is not None and corrected.draft is not None
        assert corrected.draft.id == identifier
        assert corrected.draft.status == "AWAITING_CONFIRMATION"
        assert corrected.draft.missing_fields_json == []
        assert corrected.draft.fields_json["conta_casa_id"] == account_id
        assert "Dados completos" in corrected.text
        assert corrected.draft.version == 2
        history = await RascunhoApostaRepo().history(session, identifier)
        assert len(history) == 1
        assert history[0].changes_json == {"casa": "Betano", "stake_unidades": 2.0}
        assert history[0].telegram_update_id == 104
        await session.commit()

    async with como(engine_app, stranger) as session:
        assert await session.get(models.RascunhoAposta, identifier) is None
        assert await session.scalar(select(func.count()).select_from(models.RascunhoCorrecao)) == 0

    async with como(engine_app, user) as session:
        item = await session.get(models.RascunhoAposta, identifier)
        assert item is not None and item.status == "AWAITING_CONFIRMATION"
        with pytest.raises(DraftVersionConflictError):
            await RascunhoApostaRepo().save(
                session,
                item,
                expected_version=1,
                fields=dict(item.fields_json),
                metadata=dict(item.field_meta_json),
                missing=[],
                status="AWAITING_CONFIRMATION",
                changes={"odd": 2.0},
                update_id=105,
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == user)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.RascunhoAposta)
                .where(
                    models.RascunhoAposta.usuario_id == user,
                    models.RascunhoAposta.telegram_chat_id == chat,
                )
            )
            == 1
        )

    async with como(engine_app, user) as session:
        cancelled = await handle_text(
            session, user_id=user, chat_id=chat, update_id=106, text="/cancelar"
        )
        assert cancelled is not None and cancelled.draft is not None
        assert cancelled.draft.status == "CANCELLED"
        replacement = await new_photo_reply(
            session,
            user_id=user,
            chat_id=chat,
            message_id=12,
            update_id=107,
            media_file_id="new-file",
        )
        assert replacement.draft is not None and replacement.draft.id != identifier
        await session.commit()


@pytest.mark.asyncio
async def test_missing_usage_offers_account_choice_without_guessing(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user = await novo_usuario()
    async with engine_admin.begin() as conn:
        await conn.execute(insert(models.Casa).values(nome="Betano").on_conflict_do_nothing())
        house = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    assert house is not None
    async with como(engine_app, user) as session:
        account = await ContaCasaRepo().create(
            session, {"usuario_id": user, "casa_id": house, "apelido": "minha conta"}
        )
        draft, created = await open_draft(
            session,
            user_id=user,
            chat_id=711_100,
            message_id=1,
            update_id=1,
            extracted={
                "casa": "Betano",
                "odd": 1.8,
                "stake_unidades": 2,
                "data_aposta": "2026-09-20T21:00:00-03:00",
            },
        )
        assert created and draft.missing_fields_json == ["conta_casa_id"]
        await session.commit()
    async with como(engine_app, user) as session:
        response = await handle_text(
            session, user_id=user, chat_id=711_100, update_id=2, text="/continuar"
        )
        assert response is not None
        assert f"{account.id} (minha conta)" in response.text
        corrected = await handle_text(
            session,
            user_id=user,
            chat_id=711_100,
            update_id=3,
            text=f"/corrigir conta {account.id}",
        )
        assert corrected is not None and corrected.draft is not None
        assert corrected.draft.status == "AWAITING_CONFIRMATION"
        assert corrected.draft.fields_json["conta_casa_id"] == account.id
        await session.commit()


@pytest.mark.asyncio
async def test_extraction_keeps_user_corrections_and_worker_queues_photo_reply(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user = await novo_usuario()
    chat = 9_000_000 + uuid4().int % 1_000_000
    async with como(engine_app, user) as session:
        session.add(
            models.TelegramLink(
                usuario_id=user,
                telegram_user_id=chat,
                telegram_chat_id=chat,
            )
        )
        await session.commit()
    update_id = uuid4().int & ((1 << 62) - 1)
    async with como(engine_app, None) as session:
        assert await ingest_update(
            session,
            {
                "update_id": update_id,
                "message": {
                    "message_id": 77,
                    "from": {"id": chat},
                    "chat": {"id": chat, "type": "private"},
                    "photo": [{"file_id": "photo-low"}, {"file_id": "photo-high"}],
                },
            },
        )
        await session.commit()
    for _ in range(12):
        assert await process_inbox_once(engine_app)
        async with como(engine_app, user) as session:
            item = await RascunhoApostaRepo().active(session, user, chat)
            if item is not None:
                break
    else:
        pytest.fail("Telegram photo was not processed")
    assert item is not None
    async with como(engine_app, user) as session:
        item = await RascunhoApostaRepo().active(session, user, chat)
        assert item is not None and item.status == "AWAITING_EXTRACTION"
        assert item.media_reference_ciphertext is not None
        assert decrypt_payload(item.media_reference_ciphertext)["file_id"] == "photo-high"
        outbox = await session.scalar(
            select(models.TelegramOutbox).where(
                models.TelegramOutbox.usuario_id == user,
                models.TelegramOutbox.idempotency_key == f"telegram-draft:{update_id}:reply",
            )
        )
        assert outbox is not None and outbox.status == "PENDING"
        await session.commit()
    text_update_id = uuid4().int & ((1 << 62) - 1)
    async with como(engine_app, None) as session:
        assert await ingest_update(
            session,
            {
                "update_id": text_update_id,
                "message": {
                    "message_id": 78,
                    "from": {"id": chat},
                    "chat": {"id": chat, "type": "private"},
                    "text": "/continuar",
                },
            },
        )
        await session.commit()
    for _ in range(12):
        assert await process_inbox_once(engine_app)
        async with como(engine_app, user) as session:
            text_reply = await session.scalar(
                select(models.TelegramOutbox).where(
                    models.TelegramOutbox.usuario_id == user,
                    models.TelegramOutbox.idempotency_key
                    == f"telegram-draft:{text_update_id}:reply",
                )
            )
            if text_reply is not None:
                break
    else:
        pytest.fail("Telegram /continuar was not processed")
    assert text_reply is not None
    assert "pendente" in decrypt_payload(text_reply.payload_ciphertext)["text"]
    async with como(engine_app, user) as session:
        await handle_text(
            session,
            user_id=user,
            chat_id=chat,
            update_id=update_id + 1,
            text="/corrigir odd 1,9",
        )
        await session.commit()
    async with como(engine_app, user) as session:
        draft = await RascunhoApostaRepo().active(session, user, chat)
        assert draft is not None
        await apply_extraction(
            session,
            draft,
            {"odd": 1.2, "evento": "Jogo", "data_aposta": "2026-09-20T21:00:00-03:00"},
            {"odd": 0.99},
        )
        await session.commit()
    async with como(engine_app, user) as session:
        draft = await RascunhoApostaRepo().active(session, user, chat)
        assert draft is not None
        assert draft.fields_json["odd"] == pytest.approx(1.9)
        assert draft.field_meta_json["odd"]["source"] == "user"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == user)
            )
            == 0
        )
