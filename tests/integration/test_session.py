from sqlalchemy import text

from bancaemdia.core.context import current_user_id
from bancaemdia.db import session as db_session


async def test_get_db_yields_session(bound_engine) -> None:
    async for sess in db_session.get_db():
        result = await sess.execute(text("SELECT 1"))
        assert result.scalar() == 1
        break


async def test_get_db_sets_user_id(bound_engine) -> None:
    token = current_user_id.set("42")
    try:
        async for sess in db_session.get_db():
            result = await sess.execute(text("SELECT current_setting('app.current_user_id', true)"))
            assert result.scalar() == "42"
            break
    finally:
        current_user_id.reset(token)


async def test_get_read_db_yields_session(bound_engine) -> None:
    async for sess in db_session.get_read_db():
        result = await sess.execute(text("SELECT 1"))
        assert result.scalar() == 1
        break


async def test_check_db_health(bound_engine) -> None:
    assert await db_session.check_db_health() is True
