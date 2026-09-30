"""Retry persisted issuer revocations; run regularly using the server's auth DB role."""

import asyncio

from sqlalchemy import text

from bancaemdia.auth.identity_service import identity_service


async def maintain() -> None:
    service = identity_service()
    await service.expire_sessions()
    await service.drain_revocations()
    async with service.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM auth_private.flows WHERE expires_at<now()-interval '1 day'")
        )
    await service.engine.dispose()


if __name__ == "__main__":
    asyncio.run(maintain())
