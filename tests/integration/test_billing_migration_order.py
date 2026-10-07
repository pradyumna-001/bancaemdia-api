"""Both existing migration branches must install the same write guards."""

import asyncio
import importlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

if os.environ.get("BILLING_CROSS_FLOW_REQUIRED") == "1":
    importlib.import_module("bancaemdia.api.v1.telegram")
else:
    pytest.importorskip(
        "bancaemdia.api.v1.telegram",
        reason="Requires #141; mandatory Billing cross-flow CI assembles and runs these scenarios",
    )
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


@pytest.mark.parametrize("first", ["d93access2026", "b01a7b1c2026"])
async def test_billing_and_feature_migrations_guard_writes_in_either_order(
    banco, monkeypatch, first
):
    # The name is generated here and never accepts an external database identifier.
    name = "billing_order_" + uuid4().hex
    maintenance = create_async_engine(banco.url_admin, isolation_level="AUTOCOMMIT")
    target_url = make_url(banco.url_admin).set(database=name).render_as_string(hide_password=False)
    target = create_async_engine(target_url)
    async with maintenance.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        monkeypatch.setenv("DATABASE_URL", target_url)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        await asyncio.to_thread(command.upgrade, config, first)
        await asyncio.to_thread(command.upgrade, config, "head")
        async with target.begin() as conn:
            # Safe to rerun after migrations: it must not duplicate existing triggers.
            await conn.execute(text("SELECT billing_install_write_guards()"))
            installed = set(
                (
                    await conn.execute(
                        text("""
                SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                WHERE t.tgname='billing_write_guard' AND NOT t.tgisinternal
            """)
                    )
                ).scalars()
            )
            assert {
                "titulares",
                "contas_casa",
                "usos_conta_casa",
                "trocas_titular_eventos",
                "trocas_titular_requisicoes",
                "rascunhos_aposta",
                "rascunho_correcoes",
            } <= installed
            await conn.execute(
                text(
                    "INSERT INTO usuarios(id,email,nome) VALUES (1,'migration@example.invalid','Fixture')"
                )
            )
            await conn.execute(text("SELECT billing_activate_rollout()"))
            savepoint = await conn.begin_nested()
            with pytest.raises(DBAPIError) as exc:
                await conn.execute(
                    text("INSERT INTO titulares(usuario_id,nome) VALUES (1,'Blocked')")
                )
            assert exc.value.orig.sqlstate == "P0402"
            await savepoint.rollback()
    finally:
        await target.dispose()
        async with maintenance.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}"'))
        await maintenance.dispose()
