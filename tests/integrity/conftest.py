"""Explicit acceptance requires real services; diagnostics contain no credentials/raw data."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from redis.asyncio import Redis

sys.path.insert(0, str(Path(__file__).parents[1] / "integration/coleta"))
sys.path.insert(0, str(Path(__file__).parents[1] / "integration/cruzamento"))


@pytest.fixture(scope="module")
def signing_key():
    from test_pairing import signing_key

    return signing_key.__wrapped__()


@pytest.fixture
async def system(engine_app, engine_admin, novo_usuario, monkeypatch, signing_key):
    from test_pairing import system

    async for value in system.__wrapped__(
        engine_app, engine_admin, novo_usuario, monkeypatch, signing_key
    ):
        yield value


@pytest.fixture
async def api(system, monkeypatch):
    from test_contract_v2 import api

    async for value in api.__wrapped__(system, monkeypatch):
        yield value


@pytest.fixture
async def game_account(api):
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from test_consolidacao import GAME, owner

    from bancaemdia import models
    from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

    async with AsyncSession(api.engine) as session, session.begin():
        await owner(session, api.user)
        house = await session.scalar(
            select(models.ContaCasa.casa_id).where(models.ContaCasa.id == api.account)
        )
        await UsoContaCasaRepo().open(
            session, api.user, house, api.account, GAME - timedelta(days=1)
        )
    return api


@pytest.fixture(scope="session", autouse=True)
def require_services():
    assert os.environ.get("TEST_DATABASE_URL"), "Mandatory integrity suite needs PostgreSQL"
    assert os.environ.get("REDIS_URL"), "Mandatory integrity suite needs Redis"


@pytest.fixture(autouse=True)
async def redis_required():
    async with Redis.from_url(os.environ["REDIS_URL"]) as client:
        assert await client.ping()


@pytest.fixture(autouse=True)
async def persisted_diagnostics(request):
    yield
    if "api" not in request.fixturenames:
        return
    from sqlalchemy import text

    api = request.getfixturevalue("api")
    state = {
        "test": request.node.nodeid,
        "invariant": getattr(request.node, "integrity_snapshot", None),
    }
    try:
        async with api.admin.connect() as connection:
            for table in (
                "apostas",
                "coletas_casa",
                "eventos",
                "aposta_consolidacoes",
                "coleta_entregas",
            ):
                state[table] = await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE usuario_id=:u"), {"u": api.user}
                )
    except Exception as error:
        state["diagnostic_error_type"] = type(error).__name__
    directory = Path(os.environ.get("INTEGRITY_DIAGNOSTICS", "integrity-diagnostics"))
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    (directory / f"{key}-persisted.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        directory = Path(os.environ.get("INTEGRITY_DIAGNOSTICS", "integrity-diagnostics"))
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(item.nodeid.encode()).hexdigest()[:16]
        (directory / f"{key}-{report.when}.json").write_text(
            json.dumps(
                {
                    "test": item.nodeid,
                    "phase": report.when,
                    "invariant": getattr(item, "integrity_snapshot", None),
                    "seed": os.environ.get("INTEGRITY_SEED", "0"),
                    "result": "failed",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
