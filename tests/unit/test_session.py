from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError

from bancaemdia.core import context
from bancaemdia.db import session as db_session


@pytest.fixture(autouse=True)
def _reset_context():
    context.current_user_id.set(None)
    yield
    context.current_user_id.set(None)


def _fake_session_factory(conn):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return lambda *a, **k: Session()


async def _collect(agen):
    return [item async for item in agen]


async def test_get_db_sets_user_config(monkeypatch) -> None:
    executed = []

    class Conn:
        async def execute(self, stmt, params):
            executed.append((stmt, params))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, stmt, params):
            executed.append((stmt, params))

    monkeypatch.setattr(db_session, "SessionLocal", lambda: Session())
    token = context.current_user_id.set("42")
    try:
        sessions = await _collect(db_session.get_db())
    finally:
        context.current_user_id.reset(token)

    assert len(sessions) == 1
    assert executed and "app.current_user_id" in executed[0][0].text
    assert executed[0][1] == {"uid": "42"}


async def test_get_db_no_user_skips_config(monkeypatch) -> None:
    executed = []
    context.current_user_id.set(None)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, stmt, params):
            executed.append((stmt, params))

    monkeypatch.setattr(db_session, "SessionLocal", lambda: Session())
    sessions = await _collect(db_session.get_db())

    assert len(sessions) == 1
    assert executed == []


async def test_get_read_db_yields_session(monkeypatch) -> None:
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(db_session, "SessionLocal", lambda: Session())
    sessions = await _collect(db_session.get_read_db())
    assert len(sessions) == 1


async def test_check_db_health_success(monkeypatch) -> None:
    class Conn:
        async def execute(self, *args):
            return None

    class Engine:
        def begin(self):
            class _Begin:
                async def __aenter__(self):
                    return Conn()

                async def __aexit__(self, *args):
                    return False

            return _Begin()

    monkeypatch.setattr(db_session, "engine", Engine())
    assert await db_session.check_db_health() is True


async def test_check_db_health_failure(monkeypatch) -> None:
    class Conn:
        async def execute(self, *args):
            raise SQLAlchemyError("boom")

    class Engine:
        def begin(self):
            class _Begin:
                async def __aenter__(self):
                    return Conn()

                async def __aexit__(self, *args):
                    return False

            return _Begin()

    monkeypatch.setattr(db_session, "engine", Engine())
    assert await db_session.check_db_health() is False
