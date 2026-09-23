"""PostgreSQL transport contract: durable replay, transaction boundaries and recovery."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.integrations.telegram.client import TelegramApiError
from bancaemdia.integrations.telegram.polling import run_polling
from bancaemdia.integrations.telegram.webhook import ingest_update
from bancaemdia.main import app
from bancaemdia.models import TelegramInbox, TelegramLink, TelegramLinkCode, TelegramOutbox
from bancaemdia.services.telegram_link import issue_code
from bancaemdia.workers.telegram import (
    _claim_outbox,
    deliver_outbox_once,
    process_inbox_once,
)


def _update(update_id: int, sender: int, message: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "from": {"id": sender, "first_name": "Never persist this name"},
            "chat": {"id": sender, "type": "private"},
            "text": message,
        },
    }


def _update_id() -> int:
    return uuid4().int & ((1 << 62) - 1)


async def _prepare_link(
    engine_app: AsyncEngine, como, novo_usuario, *, sender: int
) -> tuple[int, int, str]:
    user = await novo_usuario()
    async with como(engine_app, user) as session:
        code = await issue_code(session, user)
        await session.commit()
    update_id = _update_id()
    async with como(engine_app, None) as session:
        assert await ingest_update(session, _update(update_id, sender, f"/vincular {code.code}"))
        await session.commit()
    return user, update_id, code.code


@pytest.mark.asyncio
async def test_ten_replays_create_one_link_and_one_queued_reply(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user, update_id, code = await _prepare_link(engine_app, como, novo_usuario, sender=990001)
    for _ in range(9):
        async with como(engine_app, None) as session:
            assert not await ingest_update(session, _update(update_id, 990001, f"/vincular {code}"))
            await session.commit()
    async with como(engine_app, None) as session:
        await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
        rows = (
            await session.scalars(select(TelegramInbox).where(TelegramInbox.update_id == update_id))
        ).all()
        assert len(rows) == 1
        assert rows[0].payload_ciphertext is not None
        assert code.encode() not in rows[0].payload_ciphertext
        assert b"Never persist this name" not in rows[0].payload_ciphertext
    assert await process_inbox_once(engine_app) is True
    async with como(engine_app, None) as session:
        await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
        inbox = await session.scalar(
            select(TelegramInbox).where(TelegramInbox.update_id == update_id)
        )
        assert inbox is not None and inbox.status == "DONE" and inbox.payload_ciphertext is None
        outbox = (
            await session.scalars(
                select(TelegramOutbox).where(
                    TelegramOutbox.idempotency_key == f"telegram-link:{update_id}:success"
                )
            )
        ).all()
        assert len(outbox) == 1 and outbox[0].status == "PENDING"
    async with como(engine_app, user) as session:
        assert (
            await session.scalar(
                select(TelegramLink).where(
                    TelegramLink.usuario_id == user,
                    TelegramLink.telegram_user_id == 990001,
                    TelegramLink.revoked_at.is_(None),
                )
            )
            is not None
        )
        stored_code = await session.scalar(
            select(TelegramLinkCode).where(TelegramLinkCode.usuario_id == user)
        )
        assert stored_code is not None and stored_code.consumed_at is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TelegramOutbox)
                .where(TelegramOutbox.usuario_id == user)
            )
            == 1
        )
        # A normal tenant context cannot inspect the global inbox.
        assert await session.scalar(select(func.count()).select_from(TelegramInbox)) == 0


class _FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, message: str) -> int:
        if self.fail:
            raise TelegramApiError("network", retryable=True)
        self.sent.append((chat_id, message))
        return 456


@pytest.mark.asyncio
async def test_outage_and_worker_crash_keep_reply_until_one_success(
    engine_app: AsyncEngine, como, novo_usuario
) -> None:
    user, update_id, _ = await _prepare_link(engine_app, como, novo_usuario, sender=990002)
    assert await process_inbox_once(engine_app)
    broken = _FakeClient(fail=True)
    assert await deliver_outbox_once(engine_app, broken) is True  # type: ignore[arg-type]
    async with como(engine_app, user) as session:
        item = await session.scalar(select(TelegramOutbox).where(TelegramOutbox.usuario_id == user))
        assert item is not None and item.status == "PENDING" and item.attempts == 1
        assert item.telegram_message_id is None
        item.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    # Claim, then lose the worker before any Telegram call. The lease makes it recoverable.
    claimed = await _claim_outbox(engine_app)
    assert claimed is not None and claimed.attempts == 2
    async with como(engine_app, user) as session:
        await session.execute(
            update(TelegramOutbox)
            .where(TelegramOutbox.usuario_id == user)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    restored = _FakeClient()
    assert await deliver_outbox_once(engine_app, restored) is True  # type: ignore[arg-type]
    assert await deliver_outbox_once(engine_app, restored) is False  # type: ignore[arg-type]
    assert restored.sent == [(990002, "Conta vinculada com sucesso.")]
    async with como(engine_app, user) as session:
        item = await session.scalar(select(TelegramOutbox).where(TelegramOutbox.usuario_id == user))
        assert item is not None and item.status == "SENT" and item.telegram_message_id == 456
        assert item.idempotency_key == f"telegram-link:{update_id}:success"


@pytest.mark.asyncio
async def test_poison_payload_goes_to_dlq_without_logging_content(
    engine_app: AsyncEngine, como
) -> None:
    update_id = _update_id()
    async with como(engine_app, None) as session:
        await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
        session.add(
            TelegramInbox(
                update_id=update_id,
                event_type="MESSAGE",
                sender_user_id=990003,
                chat_id=990003,
                message_id=1,
                payload_ciphertext=b"not-fernet",
            )
        )
        await session.commit()
    assert await process_inbox_once(engine_app) is True
    async with como(engine_app, None) as session:
        await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
        item = await session.scalar(
            select(TelegramInbox).where(TelegramInbox.update_id == update_id)
        )
        assert item is not None and item.status == "DLQ"
        assert item.attempts == 1 and item.last_error_code == "payload"


@pytest.mark.asyncio
async def test_webhook_auth_content_type_size_and_persistence(
    engine_app: AsyncEngine, monkeypatch
) -> None:
    from bancaemdia.integrations.telegram import webhook

    monkeypatch.setattr(
        webhook,
        "get_settings",
        lambda: SimpleNamespace(
            TELEGRAM_MODE="webhook", TELEGRAM_WEBHOOK_SECRET="test-webhook-secret"
        ),
    )

    async def test_db():
        async with AsyncSession(engine_app) as session:
            yield session

    monkeypatch.setattr(webhook, "get_db_primary", test_db)
    path = webhook.WEBHOOK_PATH
    update_id = _update_id()
    raw = json.dumps(_update(update_id, 990004, "/start")).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(path, content=raw, headers={"content-type": "application/json"})
        ).status_code == 403
        assert (
            await client.post(
                path,
                content=raw,
                headers={
                    webhook.SECRET_HEADER: "test-webhook-secret",
                    "content-type": "text/plain",
                },
            )
        ).status_code == 415
        assert (
            await client.post(
                path,
                content=b"x" * (webhook.MAX_WEBHOOK_BYTES + 1),
                headers={
                    webhook.SECRET_HEADER: "test-webhook-secret",
                    "content-type": "application/json",
                },
            )
        ).status_code == 413
        for _ in range(10):
            response = await client.post(
                path,
                content=raw,
                headers={
                    webhook.SECRET_HEADER: "test-webhook-secret",
                    "content-type": "application/json",
                },
            )
            assert response.status_code == 202 and response.json() == {"accepted": True}
    async with AsyncSession(engine_app) as session:
        await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TelegramInbox)
                .where(TelegramInbox.update_id == update_id)
            )
            == 1
        )


class _PollingClient:
    def __init__(self, raw: dict[str, object], *, webhook_url: str = "") -> None:
        self.raw = raw
        self.webhook_url = webhook_url
        self.offsets: list[int] = []

    async def webhook_info(self) -> dict[str, str]:
        return {"url": self.webhook_url}

    async def get_updates(self, offset: int) -> list[dict[str, object]]:
        self.offsets.append(offset)
        return [self.raw]


@pytest.mark.asyncio
async def test_local_polling_uses_same_inbox_and_refuses_active_webhook(
    engine_app: AsyncEngine, monkeypatch
) -> None:
    from bancaemdia.integrations.telegram import polling

    monkeypatch.setattr(
        polling,
        "get_settings",
        lambda: SimpleNamespace(APP_ENV="development", TELEGRAM_MODE="polling"),
    )
    update_id = _update_id()
    raw = _update(update_id, 990005, "/start")
    active = _PollingClient(raw, webhook_url="https://example.test/webhook")
    with pytest.raises(RuntimeError, match="Remove the configured"):
        await run_polling(engine_app, active, stop_after_batches=1)  # type: ignore[arg-type]
    local = _PollingClient(raw)
    await run_polling(engine_app, local, stop_after_batches=1)  # type: ignore[arg-type]
    assert local.offsets == [0]
    async with AsyncSession(engine_app) as session:
        await session.execute(text("SELECT set_config('app.telegram_transport','on',true)"))
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TelegramInbox)
                .where(TelegramInbox.update_id == update_id)
            )
            == 1
        )
