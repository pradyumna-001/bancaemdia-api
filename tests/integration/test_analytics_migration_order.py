"""Analytics rollback must preserve the independently reviewed dashboard fix."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


@pytest.mark.parametrize("first", ["b6c8e1a4d205", "f145audit2026"])
async def test_analytics_rollback_preserves_private_summary_and_reupgrades(
    banco, monkeypatch, first
):
    name = "analytics_order_" + uuid4().hex
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
        async with target.connect() as conn:
            before = await conn.scalar(text("SELECT pg_get_viewdef('painel.mv_painel_resumo')"))
            assert "apostas_antes_do_caixa" in before
            assert await conn.scalar(text("SELECT to_regclass('painel.mv_painel_resumo_legacy')"))
            triggers = set(
                (
                    await conn.execute(
                        text(
                            "SELECT tgname FROM pg_trigger WHERE tgrelid='metas_desempenho'::regclass "
                            "AND NOT tgisinternal"
                        )
                    )
                ).scalars()
            )
            assert {"active_metas_desempenho_write", "audit_metas_desempenho_write"} <= triggers
        # Remove only the analytics branch; the privacy branch stays installed.
        await asyncio.to_thread(command.downgrade, config, "b6c8e1a4d205@base")
        async with target.connect() as conn:
            assert (
                await conn.scalar(text("SELECT pg_get_viewdef('painel.mv_painel_resumo')"))
                == before
            )
            assert await conn.scalar(text("SELECT to_regclass('painel.mv_painel_resumo_legacy')"))
            assert await conn.scalar(text("SELECT to_regclass('metas_desempenho')")) is None
        await asyncio.to_thread(command.upgrade, config, "head")
        async with target.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('metas_desempenho')"))
    finally:
        await target.dispose()
        async with maintenance.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}"'))
        await maintenance.dispose()
