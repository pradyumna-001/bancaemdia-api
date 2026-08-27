from __future__ import annotations

import asyncpg
import pytest


async def _connect_as_role(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(url.replace("postgresql+asyncpg", "postgresql", 1))


async def _set_usuario_id(conn: asyncpg.Connection, value: str | None) -> None:
    if value is None:
        await conn.execute("RESET app.current_user_id")
    else:
        await conn.execute("SELECT set_config('app.current_user_id', $1, false)", value)


async def _count_visible(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("SELECT count(*) FROM test_tenant_data")


async def test_rls_filters_by_usuario_id(bancaemdia_app_role: str) -> None:
    conn = await _connect_as_role(bancaemdia_app_role)
    try:
        await _set_usuario_id(conn, "1")
        assert await _count_visible(conn) == 2

        await _set_usuario_id(conn, "2")
        assert await _count_visible(conn) == 1

        await _set_usuario_id(conn, "3")
        assert await _count_visible(conn) == 1

        await _set_usuario_id(conn, None)
        with pytest.raises(asyncpg.exceptions.InvalidTextRepresentationError):
            await _count_visible(conn)

        await _set_usuario_id(conn, "banana")
        with pytest.raises(asyncpg.exceptions.InvalidTextRepresentationError):
            await _count_visible(conn)
    finally:
        await conn.close()


async def test_rls_cross_tenant_returns_zero(bancaemdia_app_role: str) -> None:
    conn = await _connect_as_role(bancaemdia_app_role)
    try:
        await _set_usuario_id(conn, "999")
        assert await _count_visible(conn) == 0
    finally:
        await conn.close()


async def test_rls_set_local_works(bancaemdia_app_role: str) -> None:
    conn = await _connect_as_role(bancaemdia_app_role)
    try:
        await conn.execute("BEGIN")
        await conn.execute("SELECT set_config('app.current_user_id', '1', true)")
        count = await conn.fetchval("SELECT count(*) FROM test_tenant_data")
        assert count == 2
        await conn.execute("COMMIT")

        await conn.execute("BEGIN")
        await conn.execute("SELECT set_config('app.current_user_id', '2', true)")
        count = await conn.fetchval("SELECT count(*) FROM test_tenant_data")
        assert count == 1
        await conn.execute("COMMIT")
    finally:
        await conn.close()


async def test_rls_isolation_across_transactions(bancaemdia_app_role: str) -> None:
    conn = await _connect_as_role(bancaemdia_app_role)
    try:
        await conn.execute("BEGIN")
        await conn.execute("SELECT set_config('app.current_user_id', '1', true)")
        count1 = await conn.fetchval("SELECT count(*) FROM test_tenant_data")
        await conn.execute("COMMIT")

        await conn.execute("BEGIN")
        await conn.execute("SELECT set_config('app.current_user_id', '2', true)")
        count2 = await conn.fetchval("SELECT count(*) FROM test_tenant_data")
        await conn.execute("COMMIT")

        assert count1 == 2
        assert count2 == 1
        assert count1 != count2
    finally:
        await conn.close()
