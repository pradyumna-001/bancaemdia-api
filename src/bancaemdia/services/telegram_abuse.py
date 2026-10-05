"""Transactional shared limits: a retry never spends quota without its inbox commit."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.observability.metrics import telegram_abuse_total
from bancaemdia.services.telegram_link import _digest


class LimitedError(Exception):
    pass


async def admit(
    session: AsyncSession, *, action: str, sender: int, chat: int, owner: int | None
) -> bool:
    settings = get_settings()
    limits = settings.TELEGRAM_ACTION_LIMITS[action]
    identities = ("global", f"user:{owner}" if owner else f"sender:{sender}", f"chat:{chat}")
    # All three counters share the business transaction. Fixed windows start at
    # first use; row locks serialize every process and PostgreSQL owns the clock.
    try:
        async with session.begin_nested():
            for identity, limit in zip(identities, (limits[2], limits[1], limits[0]), strict=True):
                key = _digest("quota", f"{action}:{identity}")
                count = await session.scalar(
                    text("""
                        INSERT INTO telegram_rate_buckets (key, used, expires_at)
                        VALUES (:key, 1, clock_timestamp() + :seconds * interval '1 second')
                        ON CONFLICT (key) DO UPDATE SET
                          used = CASE WHEN telegram_rate_buckets.expires_at <= clock_timestamp()
                            THEN 1 ELSE telegram_rate_buckets.used + 1 END,
                          expires_at = CASE WHEN telegram_rate_buckets.expires_at <= clock_timestamp()
                            THEN clock_timestamp() + :seconds * interval '1 second'
                            ELSE telegram_rate_buckets.expires_at END
                        WHERE telegram_rate_buckets.expires_at <= clock_timestamp()
                           OR telegram_rate_buckets.used < :limit
                        RETURNING used
                    """),
                    {"key": key, "seconds": settings.TELEGRAM_LIMIT_WINDOW_SECONDS, "limit": limit},
                )
                if count is None:
                    raise LimitedError
    except LimitedError:
        telegram_abuse_total.labels(action=action, outcome="limited").inc()
        return False
    telegram_abuse_total.labels(action=action, outcome="accepted").inc()
    return True
