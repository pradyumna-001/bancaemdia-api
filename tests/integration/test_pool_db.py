import asyncio

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from bancaemdia.db.session import prewarm_pool
from bancaemdia.workers.coleta_runtime import CollectionRuntime

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


async def test_collection_runner_reuses_real_connection_and_resets_transaction_tenant(banco):
    def exercise():
        engine = create_async_engine(banco.url_app, pool_size=1, max_overflow=0, pool_pre_ping=True)
        runtime = CollectionRuntime(engine)
        connected = []
        event.listen(
            engine.sync_engine, "connect", lambda connection, record: connected.append(record)
        )

        async def query(usuario_id):
            async with engine.connect() as connection:
                assert await connection.scalar(
                    text("SELECT current_setting('app.current_user_id', true)")
                ) in (None, "")
                await connection.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(usuario_id)},
                )
                return await connection.scalar(text("SELECT pg_backend_pid()"))

        try:
            first = runtime.run(query(1))
            assert runtime.run(query(2)) == first
            assert len(connected) == 1
        finally:
            runtime.close()

    await asyncio.to_thread(exercise)
