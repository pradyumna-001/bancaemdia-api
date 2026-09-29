"""Bounded, repeatable retention. Pending work is never removed by a timer."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.models import RascunhoAposta, RascunhoCorrecao, TelegramInbox, TelegramOutbox
from bancaemdia.models.telegram_media import TelegramMedia
from bancaemdia.services.telegram_link import _digest
from bancaemdia.workers.celery_app import app
from bancaemdia.workers.materialization import get_engine


async def purge_telegram(engine: AsyncEngine) -> int:
    settings = get_settings()
    now = datetime.now(UTC)
    raw_cutoff = now - timedelta(days=settings.TELEGRAM_RAW_RETENTION_DAYS)
    media_cutoff = now - timedelta(days=settings.TELEGRAM_MEDIA_RETENTION_DAYS)
    draft_cutoff = now - timedelta(days=settings.TELEGRAM_DRAFT_RETENTION_DAYS)
    limit = settings.TELEGRAM_PURGE_BATCH_SIZE
    count = 0
    async with AsyncSession(engine) as session, session.begin():
        await session.execute(text("SELECT set_config('app.telegram_transport', 'on', true)"))
        for model, statuses in (
            (TelegramInbox, ("DONE", "DLQ")),
            (TelegramOutbox, ("SENT", "DLQ")),
        ):
            rows = (
                await session.scalars(
                    select(model)
                    .where(
                        model.status.in_(statuses),
                        model.created_at < raw_cutoff,
                        model.chat_id.is_not(None)
                        if model is TelegramInbox
                        else model.chat_id != 0,
                    )
                    .order_by(model.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
            for row in rows:
                if isinstance(row, TelegramInbox):
                    row.payload_ciphertext = None
                    row.sender_user_id = row.chat_id = row.message_id = None
                elif isinstance(row, TelegramOutbox):
                    row.payload_ciphertext = b""
                    row.chat_id = 0
                    row.telegram_message_id = None
                count += 1
        await session.execute(
            text("""
            DELETE FROM telegram_rate_buckets WHERE key IN (
              SELECT key FROM telegram_rate_buckets WHERE expires_at < clock_timestamp()
              ORDER BY expires_at LIMIT :limit FOR UPDATE SKIP LOCKED)
        """),
            {"limit": limit},
        )
        drafts = (
            await session.scalars(
                select(RascunhoAposta)
                .where(
                    RascunhoAposta.closed_at.is_not(None),
                    RascunhoAposta.closed_at < max(media_cutoff, draft_cutoff),
                    RascunhoAposta.purged_at.is_(None),
                )
                .order_by(RascunhoAposta.closed_at)
                .limit(limit)
            )
        ).all()
        for candidate in drafts:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"),
                {"uid": str(candidate.usuario_id)},
            )
            draft = await session.scalar(
                select(RascunhoAposta)
                .where(
                    RascunhoAposta.id == candidate.id,
                )
                .with_for_update(skip_locked=True)
            )
            if draft is None or draft.closed_at is None:
                continue
            if draft.closed_at < media_cutoff:
                await session.execute(
                    delete(TelegramMedia).where(TelegramMedia.draft_id == draft.id)
                )
                if draft.media_hash:
                    await session.execute(
                        text("SELECT telegram_purge_legacy_media(:hash, :cutoff)"),
                        {"hash": draft.media_hash, "cutoff": media_cutoff},
                    )
                draft.media_reference_ciphertext = None
                draft.media_hash = None
                draft.source_metadata_json = {}
            if draft.closed_at < draft_cutoff and draft.closed_at < media_cutoff:
                await session.execute(
                    delete(RascunhoCorrecao).where(RascunhoCorrecao.rascunho_id == draft.id)
                )
                draft.origin_digest = draft.origin_digest or _digest(
                    "origin",
                    f"{draft.usuario_id}:{draft.telegram_chat_id}:{draft.telegram_message_id}",
                )
                draft.telegram_chat_id = 0
                draft.telegram_message_id = draft.telegram_update_id
                draft.fields_json = draft.field_meta_json = {}
                draft.missing_fields_json = []
                draft.coupon_candidates_json = []
                draft.purged_at = now
            await session.flush()
            count += 1
    return count


def telegram_purge() -> None:
    try:
        asyncio.run(purge_telegram(get_engine()))
    except Exception:
        raise RuntimeError("telegram purge failed") from None


telegram_purge_task = app.task(name="telegram.purge", ignore_result=True)(telegram_purge)
