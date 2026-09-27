"""PostgreSQL-backed Telegram inbox/outbox pump; Celery ticks are only wakeups."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.integrations.telegram.client import TelegramApiError, TelegramClient
from bancaemdia.integrations.telegram.codec import (
    InvalidTelegramUpdateError,
    decrypt_payload,
    encrypt_payload,
)
from bancaemdia.models import TelegramInbox, TelegramOutbox
from bancaemdia.observability.metrics import (
    telegram_inbox_depth,
    telegram_inbox_oldest_age_seconds,
    telegram_outbox_depth,
    telegram_outbox_oldest_age_seconds,
    telegram_transport_dlq_count,
    telegram_transport_retries_total,
)
from bancaemdia.services.telegram_conversation import handle_text
from bancaemdia.services.telegram_link import REPO, IncomingCommand, redeem_command, resolve_sender
from bancaemdia.services.telegram_photo_intake import SUPPORTED_FLOW, intake_photo
from bancaemdia.workers.celery_app import app
from bancaemdia.workers.materialization import get_engine

MAX_ATTEMPTS = 8
LEASE_SECONDS = 60
BATCH_SIZE = 25


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(attempts: int, *, floor: int = 0) -> timedelta:
    return timedelta(seconds=max(floor, min(300, 2 ** min(attempts, 8))))


async def _transport_scope(session: AsyncSession) -> None:
    await session.execute(text("SELECT set_config('app.telegram_transport', 'on', true)"))


async def queue_reply(
    session: AsyncSession, *, user_id: int, chat_id: int, key: str, message: str
) -> None:
    """Enqueue inside the same transaction as the originating business effect."""
    if not key or len(key) > 128 or not message or len(message) > 4096:
        raise ValueError("invalid telegram reply")
    await session.execute(
        insert(TelegramOutbox)
        .values(
            usuario_id=user_id,
            idempotency_key=key,
            chat_id=chat_id,
            payload_ciphertext=encrypt_payload({"text": message}),
        )
        .on_conflict_do_nothing(
            index_elements=[TelegramOutbox.usuario_id, TelegramOutbox.idempotency_key]
        )
    )


async def _handle_inbox(session: AsyncSession, item: TelegramInbox) -> None:
    if item.payload_ciphertext is None:
        return
    payload = decrypt_payload(item.payload_ciphertext)
    sender = item.sender_user_id
    chat = item.chat_id
    if item.event_type != "MESSAGE" or sender is None or chat is None:
        return
    chat_type = payload.get("chat_type")
    message_text = payload.get("text")
    if isinstance(message_text, str) and message_text.strip().lower().startswith("/vincular"):
        linked = await redeem_command(
            session,
            IncomingCommand(
                chat_type=str(chat_type),
                sender_user_id=sender,
                chat_id=chat,
                text=message_text,
            ),
            commit=False,
        )
        if linked:
            owner = await REPO.active_owner(session, sender, chat)
            if owner is not None:
                await queue_reply(
                    session,
                    user_id=owner,
                    chat_id=chat,
                    key=f"telegram-link:{item.update_id}:success",
                    message="Conta vinculada com sucesso.",
                )
        return
    if chat_type == "private":
        owner = await resolve_sender(session, sender, chat)
        if owner is None:
            return
        photos = payload.get("photo")
        if (photos or payload.get("media_group_id")) and item.message_id is not None:
            photo_reply = await intake_photo(
                session,
                user_id=owner,
                chat_id=chat,
                message_id=item.message_id,
                update_id=item.update_id,
                payload=payload,
            )
            await queue_reply(
                session,
                user_id=owner,
                chat_id=chat,
                key=f"telegram-draft:{item.update_id}:reply",
                message=photo_reply.text,
            )
            return
        if isinstance(message_text, str):
            if message_text.strip().lower() in {"/confirmar", "confirmar"}:
                from bancaemdia.services.telegram_confirmation import confirm_draft

                confirmation = await confirm_draft(
                    session, user_id=owner, chat_id=chat, update_id=item.update_id
                )
                if not confirmation.queued:
                    await queue_reply(
                        session,
                        user_id=owner,
                        chat_id=chat,
                        key=f"telegram-draft:{item.update_id}:reply",
                        message=confirmation.text,
                    )
                return
            text_reply = await handle_text(
                session,
                user_id=owner,
                chat_id=chat,
                update_id=item.update_id,
                text=message_text,
            )
            if text_reply is not None or message_text.strip():
                await queue_reply(
                    session,
                    user_id=owner,
                    chat_id=chat,
                    key=f"telegram-draft:{item.update_id}:reply",
                    message=text_reply.text if text_reply is not None else SUPPORTED_FLOW,
                )


async def process_inbox_once(engine: AsyncEngine) -> bool:
    item_id: int | None = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            async with session.begin():
                await _transport_scope(session)
                item = await session.scalar(
                    select(TelegramInbox)
                    .where(
                        TelegramInbox.status == "PENDING",
                        TelegramInbox.next_attempt_at <= func.clock_timestamp(),
                    )
                    .order_by(TelegramInbox.next_attempt_at, TelegramInbox.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if item is None:
                    return False
                item_id = item.id
                await _handle_inbox(session, item)
                item.status = "DONE"
                item.processed_at = _now()
                item.payload_ciphertext = None
        return True
    except Exception as exc:
        if item_id is None:
            raise
        # The failed business transaction has rolled back. Only safe reason codes survive.
        reason = "payload" if isinstance(exc, InvalidTelegramUpdateError) else "processing"
        async with AsyncSession(engine, expire_on_commit=False) as session:
            async with session.begin():
                await _transport_scope(session)
                item = await session.scalar(
                    select(TelegramInbox)
                    .where(
                        TelegramInbox.id == item_id,
                        TelegramInbox.status == "PENDING",
                    )
                    .with_for_update()
                )
                if item is not None:
                    item.attempts += 1
                    item.last_error_code = reason
                    if item.attempts >= MAX_ATTEMPTS or reason == "payload":
                        item.status = "DLQ"
                    else:
                        item.next_attempt_at = _now() + _backoff(item.attempts)
                        telegram_transport_retries_total.labels(stage="inbox").inc()
        structlog.get_logger(__name__).warning("telegram_inbox_retry", reason=reason)
        return True


@dataclass(frozen=True)
class OutboxClaim:
    id: int
    user_id: int
    chat_id: int
    ciphertext: bytes
    attempts: int
    token: str


async def _claim_outbox(engine: AsyncEngine, *, outbox_id: int | None = None) -> OutboxClaim | None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await _transport_scope(session)
            item = await session.scalar(
                select(TelegramOutbox)
                .where(
                    *([TelegramOutbox.id == outbox_id] if outbox_id is not None else []),
                    or_(
                        (TelegramOutbox.status == "PENDING")
                        & (TelegramOutbox.next_attempt_at <= func.clock_timestamp()),
                        (TelegramOutbox.status == "SENDING")
                        & (TelegramOutbox.lease_until <= func.clock_timestamp()),
                    ),
                )
                .order_by(TelegramOutbox.next_attempt_at, TelegramOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if item is None:
                return None
            if item.attempts >= MAX_ATTEMPTS:
                item.status = "DLQ"
                item.last_error_code = "lease_exhausted"
                item.lease_until = None
                item.lease_token = None
                return None
            token = uuid4().hex
            item.status = "SENDING"
            item.attempts += 1
            item.lease_until = _now() + timedelta(seconds=LEASE_SECONDS)
            item.lease_token = token
            return OutboxClaim(
                item.id,
                item.usuario_id,
                item.chat_id,
                item.payload_ciphertext,
                item.attempts,
                token,
            )


async def _finish_outbox(
    engine: AsyncEngine,
    claim: OutboxClaim,
    *,
    message_id: int | None = None,
    error: TelegramApiError | InvalidTelegramUpdateError | None = None,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await _transport_scope(session)
            item = await session.scalar(
                select(TelegramOutbox)
                .where(
                    TelegramOutbox.id == claim.id,
                    TelegramOutbox.status == "SENDING",
                    TelegramOutbox.lease_token == claim.token,
                )
                .with_for_update()
            )
            if item is None:
                return
            item.lease_until = None
            item.lease_token = None
            if error is None:
                item.status = "SENT"
                item.sent_at = _now()
                item.telegram_message_id = message_id
                item.last_error_code = None
                return
            item.last_error_code = (
                "payload" if isinstance(error, InvalidTelegramUpdateError) else error.reason
            )
            if (
                item.attempts >= MAX_ATTEMPTS
                or isinstance(error, InvalidTelegramUpdateError)
                or not error.retryable
            ):
                item.status = "DLQ"
            else:
                item.status = "PENDING"
                floor = error.retry_after if isinstance(error, TelegramApiError) else 0
                item.next_attempt_at = _now() + _backoff(item.attempts, floor=floor)
                telegram_transport_retries_total.labels(stage="outbox").inc()


async def deliver_outbox_once(
    engine: AsyncEngine, client: TelegramClient, *, outbox_id: int | None = None
) -> bool:
    claim = await _claim_outbox(engine, outbox_id=outbox_id)
    if claim is None:
        return False
    try:
        payload = decrypt_payload(claim.ciphertext)
        message = payload.get("text")
        if not isinstance(message, str):
            raise InvalidTelegramUpdateError("invalid reply payload")
        message_id = await client.send_message(claim.chat_id, message)
    except (TelegramApiError, InvalidTelegramUpdateError) as exc:
        await _finish_outbox(engine, claim, error=exc)
    else:
        await _finish_outbox(engine, claim, message_id=message_id)
    return True


async def refresh_telegram_metrics(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as session:
        await _transport_scope(session)
        for table, pending, depth, age, stage in (
            (
                "telegram_inbox",
                "status = 'PENDING'",
                telegram_inbox_depth,
                telegram_inbox_oldest_age_seconds,
                "inbox",
            ),
            (
                "telegram_outbox",
                "status IN ('PENDING','SENDING')",
                telegram_outbox_depth,
                telegram_outbox_oldest_age_seconds,
                "outbox",
            ),
        ):
            row = (
                await session.execute(
                    text(
                        f"SELECT count(*) FILTER (WHERE {pending}), "
                        f"coalesce(extract(epoch from clock_timestamp() - min(created_at) FILTER (WHERE {pending})), 0), "
                        f"count(*) FILTER (WHERE status = 'DLQ') FROM {table}"
                    )
                )
            ).one()
            depth.set(int(row[0]))
            age.set(max(0.0, float(row[1])))
            telegram_transport_dlq_count.labels(stage=stage).set(int(row[2]))


async def run_cycle(
    engine: AsyncEngine, client: TelegramClient, *, limit: int = BATCH_SIZE
) -> None:
    from bancaemdia.workers.telegram_extraction import process_photo_once

    for _ in range(limit):
        if not await process_inbox_once(engine):
            break
    for _ in range(limit):
        if not await process_photo_once(engine, client):
            break
    for _ in range(limit):
        if not await deliver_outbox_once(engine, client):
            break
    await refresh_telegram_metrics(engine)


def telegram_tick() -> None:
    async def run() -> None:
        try:
            client = TelegramClient()
        except TelegramApiError:
            # Inbox processing still runs without a bot token; outbox remains durable.
            for _ in range(BATCH_SIZE):
                if not await process_inbox_once(get_engine()):
                    break
            await refresh_telegram_metrics(get_engine())
            return
        try:
            await run_cycle(get_engine(), client)
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
    except Exception:
        # Celery serializes exception reprs and tracebacks. Provider URLs include
        # the bot token, so never let an unexpected transport error reach it.
        structlog.get_logger(__name__).error("telegram_tick_failed")
        raise RuntimeError("telegram transport cycle failed") from None


telegram_tick_task = app.task(name="telegram.tick", ignore_result=True)(telegram_tick)
