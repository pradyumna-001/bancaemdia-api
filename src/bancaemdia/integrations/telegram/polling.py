"""Local-only Telegram long polling using the same durable inbox as the webhook."""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.integrations.telegram.client import TelegramApiError, TelegramClient
from bancaemdia.integrations.telegram.codec import InvalidTelegramUpdateError
from bancaemdia.integrations.telegram.webhook import ingest_update
from bancaemdia.workers.materialization import get_engine


async def run_polling(
    engine: AsyncEngine | None = None,
    client: TelegramClient | None = None,
    *,
    stop_after_batches: int | None = None,
) -> None:
    settings = get_settings()
    if settings.APP_ENV == "production" or settings.TELEGRAM_MODE != "polling":
        raise RuntimeError("Telegram polling is enabled only in local polling mode")
    owned_client = client is None
    active_client = client or TelegramClient()
    try:
        info = await active_client.webhook_info()
        if info.get("url"):
            raise RuntimeError("Remove the configured Telegram webhook before local polling")
        active_engine = engine or get_engine()
        offset = 0
        batches = 0
        while stop_after_batches is None or batches < stop_after_batches:
            try:
                updates = await active_client.get_updates(offset)
            except TelegramApiError as exc:
                structlog.get_logger(__name__).warning("telegram_poll_retry", reason=exc.reason)
                await asyncio.sleep(max(1, exc.retry_after))
                continue
            for raw in updates:
                try:
                    async with AsyncSession(active_engine) as session:
                        async with session.begin():
                            await ingest_update(session, raw)
                except InvalidTelegramUpdateError:
                    # Never confirm an invalid provider update by advancing offset.
                    raise
                value = raw.get("update_id")
                if type(value) is int:
                    offset = max(offset, value + 1)
            batches += 1
    finally:
        if owned_client:
            await active_client.aclose()


if __name__ == "__main__":
    asyncio.run(run_polling())
