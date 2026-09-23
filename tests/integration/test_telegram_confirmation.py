"""PostgreSQL contract for explicit, atomic and replay-safe draft confirmation."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from bancaemdia import models
from bancaemdia.integrations.telegram.codec import decrypt_payload
from bancaemdia.integrations.telegram.webhook import ingest_update
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo
from bancaemdia.services.telegram_confirmation import confirm_draft
from bancaemdia.services.telegram_conversation import handle_text, open_draft
from bancaemdia.workers.telegram import process_inbox_once
from bancaemdia.workers.telegram_materialization import draft_bet_key

pytestmark = pytest.mark.xdist_group("postgres")


def _id() -> int:
    return uuid4().int & ((1 << 62) - 1)


async def _account(engine_admin: AsyncEngine, engine_app: AsyncEngine, como, user: int) -> int:
    async with engine_admin.begin() as conn:
        await conn.execute(insert(models.Casa).values(nome="Betano").on_conflict_do_nothing())
        house_id = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    assert house_id is not None
    async with como(engine_app, user) as session:
        account = await ContaCasaRepo().create(
            session,
            {"usuario_id": user, "casa_id": house_id, "apelido": "principal", "estado": "EM_USO"},
        )
        await UsoContaCasaRepo().open(
            session, user, house_id, account.id, datetime(2026, 1, 1, tzinfo=UTC)
        )
        await session.commit()
    return account.id


async def _draft(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como,
    novo_usuario,
    *,
    complete: bool = True,
) -> tuple[int, int, models.RascunhoAposta, int]:
    user = await novo_usuario()
    chat = 9_100_000_000 + uuid4().int % 1_000_000
    account_id = await _account(engine_admin, engine_app, como, user)
    async with como(engine_app, user) as session:
        session.add(
            models.TelegramLink(usuario_id=user, telegram_user_id=chat, telegram_chat_id=chat)
        )
        draft, created = await open_draft(
            session,
            user_id=user,
            chat_id=chat,
            message_id=71,
            update_id=_id(),
            media_file_id="photo-file-id",
            media_hash="a" * 64,
            extracted={
                "casa": "Betano",
                "odd": 1.92,
                "evento": "Corinthians x Santos",
                "descricao": "Corinthians vence",
                "mercado_bruto": "Resultado final",
                "stake_unidades": 2.0 if complete else None,
                "data_aposta": "2026-09-20T21:00:00-03:00",
            },
            confidence={"casa": 0.99, "odd": 0.99},
        )
        assert created
        draft_id = draft.id
        await session.commit()
    async with como(engine_app, user) as session:
        persisted = await session.get(models.RascunhoAposta, draft_id)
        assert persisted is not None
        return user, chat, persisted, account_id


@pytest.mark.asyncio
async def test_ten_concurrent_confirmations_create_one_bet_event_and_reply(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user, chat, draft, account_id = await _draft(engine_admin, engine_app, como, novo_usuario)
    # The export path can own the same positive Telegram coordinates. Bot confirmation uses
    # only its draft UUID as its financial key, while the source coordinates stay in the event.
    async with como(engine_app, user) as session:
        await ApostaRepo().upsert_idempotent(
            session,
            {
                "usuario_id": user,
                "chave": f"t:{chat}:71:0",
                "origem": "telegram",
                "chat_id": chat,
                "message_id": 71,
                "stake_unidades": 1.0,
                "stake_centavos": 10_000,
                "odd": 1.5,
            },
        )
        await session.commit()

    async def attempt() -> str:
        async with como(engine_app, user) as session:
            reply = await confirm_draft(session, user_id=user, chat_id=chat, update_id=_id())
            await session.commit()
            assert reply.queued
            return reply.text

    replies = await asyncio.gather(*(attempt() for _ in range(10)))
    assert len(set(replies)) == 1
    key = draft_bet_key(draft.id)
    async with como(engine_app, user) as session:
        persisted = await session.get(models.RascunhoAposta, draft.id)
        assert persisted is not None and persisted.status == "CONFIRMED"
        bet = await ApostaRepo().get_by_chave(session, user, key)
        assert bet is not None and bet.origem == "telegram_bot"
        assert bet.chat_id is None and bet.message_id is None
        assert bet.conta_casa_id == account_id
        assert bet.odd == pytest.approx(1.92)
        assert bet.stake_unidades == pytest.approx(2.0)
        assert bet.stake_centavos == 20_000
        assert f"#{bet.id}" in replies[0]
        assert f"{bet.stake_centavos} centavos" in replies[0]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == user, models.Aposta.chave == key)
            )
            == 1
        )
        events = (
            await session.scalars(
                select(models.Evento).where(
                    models.Evento.usuario_id == user, models.Evento.aposta_chave == key
                )
            )
        ).all()
        assert len([event for event in events if event.tipo == "APOSTA_CRIADA"]) == 1
        source = next(event for event in events if event.tipo == "APOSTA_CRIADA")
        assert source.fonte == "manual"
        assert source.payload_json["telegram_draft_id"] == str(draft.id)
        assert source.payload_json["conta_casa_id"] == account_id
        outboxes = (
            await session.scalars(
                select(models.TelegramOutbox).where(
                    models.TelegramOutbox.usuario_id == user,
                    models.TelegramOutbox.idempotency_key
                    == f"telegram-confirmation:{draft.id}:success",
                )
            )
        ).all()
        assert len(outboxes) == 1
        assert decrypt_payload(outboxes[0].payload_ciphertext)["text"] == replies[0]


@pytest.mark.asyncio
async def test_inbox_requires_explicit_confirm_and_replay_uses_saved_values(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user, chat, draft, _ = await _draft(engine_admin, engine_app, como, novo_usuario)
    ambiguous = await handle_text_in_session(engine_app, como, user, chat, "sim")
    assert ambiguous is not None and "corrigir" in ambiguous.lower()
    async with como(engine_app, user) as session:
        assert await ApostaRepo().get_by_chave(session, user, draft_bet_key(draft.id)) is None
    update_id = _id()
    async with como(engine_app, None) as session:
        assert await ingest_update(
            session,
            {
                "update_id": update_id,
                "message": {
                    "message_id": 72,
                    "from": {"id": chat},
                    "chat": {"id": chat, "type": "private"},
                    "text": "/confirmar",
                },
            },
        )
        await session.commit()
    for _ in range(30):
        assert await process_inbox_once(engine_app)
        async with como(engine_app, user) as session:
            persisted = await session.get(models.RascunhoAposta, draft.id)
            if persisted is not None and persisted.status == "CONFIRMED":
                break
    else:
        pytest.fail("confirmation update not processed")
    async with como(engine_app, user) as session:
        bet = await ApostaRepo().get_by_chave(session, user, draft_bet_key(draft.id))
        assert bet is not None
        reply = await confirm_draft(session, user_id=user, chat_id=chat, update_id=_id())
        assert reply.queued and f"#{bet.id}" in reply.text
        await session.commit()


async def handle_text_in_session(engine_app, como, user: int, chat: int, text: str) -> str | None:
    async with como(engine_app, user) as session:
        reply = await handle_text(session, user_id=user, chat_id=chat, update_id=_id(), text=text)
        await session.commit()
        return None if reply is None else reply.text


@pytest.mark.asyncio
async def test_incomplete_draft_and_failed_outbox_never_commit_finance(
    engine_admin: AsyncEngine, engine_app: AsyncEngine, como, novo_usuario, monkeypatch
) -> None:
    user, chat, draft, _ = await _draft(
        engine_admin, engine_app, como, novo_usuario, complete=False
    )
    async with como(engine_app, user) as session:
        reply = await confirm_draft(session, user_id=user, chat_id=chat, update_id=_id())
        assert not reply.queued and "stake" in reply.text
        await session.commit()
    async with como(engine_app, user) as session:
        corrected = await handle_text(
            session, user_id=user, chat_id=chat, update_id=_id(), text="stake=2"
        )
        assert corrected is not None and corrected.draft is not None
        assert corrected.draft.status == "AWAITING_CONFIRMATION"
        await session.commit()

    from bancaemdia.services import telegram_confirmation

    async def broken_queue(*args, **kwargs):
        await asyncio.sleep(0)
        raise RuntimeError("outbox unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(telegram_confirmation, "queue_reply", broken_queue)
        with pytest.raises(RuntimeError, match="outbox unavailable"):
            async with como(engine_app, user) as session:
                await confirm_draft(session, user_id=user, chat_id=chat, update_id=_id())
                await session.commit()
    async with como(engine_app, user) as session:
        persisted = await session.get(models.RascunhoAposta, draft.id)
        assert persisted is not None and persisted.status == "AWAITING_CONFIRMATION"
        assert await ApostaRepo().get_by_chave(session, user, draft_bet_key(draft.id)) is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(
                    models.Evento.usuario_id == user,
                    models.Evento.aposta_chave == draft_bet_key(draft.id),
                )
            )
            == 0
        )
    async with como(engine_app, user) as session:
        recovered = await confirm_draft(session, user_id=user, chat_id=chat, update_id=_id())
        assert recovered.queued
        await session.commit()
    async with como(engine_app, user) as session:
        assert await ApostaRepo().get_by_chave(session, user, draft_bet_key(draft.id)) is not None
