import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from bancaemdia.db.session import prewarm_pool

pytestmark = pytest.mark.xdist_group("postgres")


async def test_prewarmed_connections_are_reused_without_transaction_tenant_leaks(banco):
    engine = create_async_engine(banco.url_app, pool_size=3, max_overflow=0)
    connections = []
    event.listen(
        engine.sync_engine, "connect", lambda connection, record: connections.append(record)
    )

    async def wave():
        barrier = asyncio.Barrier(3)

        async def borrow(usuario_id):
            async with engine.connect() as connection:
                await barrier.wait()
                assert await connection.scalar(
                    text("SELECT current_setting('app.current_user_id', true)")
                ) in (None, "")
                await connection.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(usuario_id)},
                )
                return await connection.scalar(text("SELECT pg_backend_pid()"))

        return set(await asyncio.gather(*(borrow(uid) for uid in range(1, 4))))

    try:
        await prewarm_pool(engine, 3)
        assert len(connections) == 3
        first = await wave()
        assert len(first) == 3
        assert await wave() == first
        assert len(connections) == 3
    finally:
        await engine.dispose()
