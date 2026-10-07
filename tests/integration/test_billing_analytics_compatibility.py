"""SQL access guards cover analytics goals in both independently published orders."""

import asyncio
import importlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

if os.environ.get("API_REVIEW_QUEUE_REQUIRED") == "1":
    importlib.import_module("bancaemdia.models.meta_desempenho")
else:
    pytest.importorskip(
        "bancaemdia.models.meta_desempenho",
        reason="Requires #158; mandatory API review-queue CI executes both migration orders",
    )

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


@pytest.mark.parametrize("first", ["f154access2026", "f158privacy2026"])
async def test_billing_guards_goals_in_both_orders_and_after_analytics_rollback(
    banco, monkeypatch, first
):
    name = "goals_order_" + uuid4().hex
    maintenance = create_async_engine(banco.url_admin, isolation_level="AUTOCOMMIT")
    url = make_url(banco.url_admin).set(database=name).render_as_string(hide_password=False)
    target = create_async_engine(url)
    async with maintenance.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        await asyncio.to_thread(command.upgrade, config, first)
        await asyncio.to_thread(command.upgrade, config, "head")
        async with target.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO usuarios(id,email,nome) VALUES (1,'goals@example.invalid','Fixture')"
                )
            )
            insert = """INSERT INTO metas_desempenho
                (usuario_id,titulo,metrica,inicio,fim,alvo,linha_base,status)
                VALUES (1,'Goal','total_apostas',current_date,current_date,10,0,'ativa')"""
            await conn.execute(text(insert))
            await conn.execute(text("SELECT billing_activate_rollout()"))
            for statement in (
                insert,
                "UPDATE metas_desempenho SET alvo=20 WHERE usuario_id=1",
                "DELETE FROM metas_desempenho WHERE usuario_id=1",
            ):
                savepoint = await conn.begin_nested()
                with pytest.raises(DBAPIError) as exc:
                    await conn.execute(text(statement))
                assert exc.value.orig.sqlstate == "P0402"
                await savepoint.rollback()
            assert await conn.scalar(text("SELECT count(*) FROM metas_desempenho")) == 1
        await asyncio.to_thread(command.downgrade, config, "b6c8e1a4d205@a9d6e3f1c210")
        await asyncio.to_thread(command.upgrade, config, "head")
        async with target.connect() as conn:
            assert await conn.scalar(
                text("""SELECT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid='metas_desempenho'::regclass
                    AND tgname='billing_write_guard' AND NOT tgisinternal)""")
            )
    finally:
        await target.dispose()
        async with maintenance.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}"'))
        await maintenance.dispose()
