from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

pytestmark = pytest.mark.xdist_group("postgres")

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "f2a9c4e7b106"
APP_ROLE = "bancaemdia_app"
MATERIALIZED_VIEWS = (
    "mv_painel_resumo",
    "mv_painel_por_casa",
    "mv_painel_por_tipster",
    "mv_painel_por_mercado",
    "mv_painel_por_periodo",
    "mv_painel_evolucao_banca",
)

Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]


async def _insert_bet(
    engine: AsyncEngine,
    como: Como,
    usuario_id: int,
    *,
    estado: str,
    stake_centavos: int,
    retorno_centavos: int | None,
    valor_aposta_centavos: int | None = None,
    freebet: bool = False,
    selecionada: bool = True,
    revisao_grave: bool = False,
) -> None:
    async with como(engine, usuario_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO apostas (
                    usuario_id,
                    chave,
                    origem,
                    data_aposta,
                    stake_unidades,
                    stake_centavos,
                    valor_aposta_centavos,
                    retorno_centavos,
                    estado,
                    freebet,
                    selecionada,
                    revisao_grave
                ) VALUES (
                    :usuario_id,
                    :chave,
                    'manual',
                    '2026-09-21 15:00:00+00',
                    1,
                    :stake_centavos,
                    :valor_aposta_centavos,
                    :retorno_centavos,
                    :estado,
                    :freebet,
                    :selecionada,
                    :revisao_grave
                )
                """
            ),
            {
                "usuario_id": usuario_id,
                "chave": f"m:{uuid4().hex}",
                "stake_centavos": stake_centavos,
                "valor_aposta_centavos": (
                    stake_centavos if valor_aposta_centavos is None else valor_aposta_centavos
                ),
                "retorno_centavos": retorno_centavos,
                "estado": estado,
                "freebet": freebet,
                "selecionada": selecionada,
                "revisao_grave": revisao_grave,
            },
        )
        await session.commit()


async def _refresh(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        # This is the exact ordering used by the optional pg_cron command and the CLI.
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        await connection.execute(text("SET LOCAL statement_timeout = 0"))
        await connection.execute(text("SELECT pg_advisory_xact_lock(20260930)"))
        for name in MATERIALIZED_VIEWS:
            await connection.execute(text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY painel.{name}"))
        await connection.execute(
            text("UPDATE painel.estado_refresh SET atualizado_em = clock_timestamp() WHERE id = 1")
        )


async def test_public_wrappers_enforce_tenant_isolation_and_hide_everything_without_context(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    ana, bia = await novo_usuario(), await novo_usuario()
    await _insert_bet(
        engine_app,
        como,
        ana,
        estado="GREEN",
        stake_centavos=10_000,
        retorno_centavos=19_000,
    )
    await _insert_bet(
        engine_app,
        como,
        bia,
        estado="RED",
        stake_centavos=7_000,
        retorno_centavos=0,
    )
    await _refresh(engine_admin)

    async with como(engine_app, ana) as session:
        rows = (
            (await session.execute(text("SELECT usuario_id FROM public.painel_resumo")))
            .scalars()
            .all()
        )
        updated_at = await session.scalar(
            text("SELECT atualizado_em FROM public.painel_atualizacao")
        )
    async with como(engine_app, bia) as session:
        bia_rows = (
            (await session.execute(text("SELECT usuario_id FROM public.painel_resumo")))
            .scalars()
            .all()
        )
    async with como(engine_app, None) as session:
        anonymous_rows = await session.scalar(text("SELECT count(*) FROM public.painel_resumo"))
        anonymous_state = await session.scalar(
            text("SELECT count(*) FROM public.painel_atualizacao")
        )

    assert rows == [ana]
    assert bia_rows == [bia]
    assert updated_at is not None
    assert (anonymous_rows, anonymous_state) == (0, 0)


async def test_the_app_role_cannot_read_the_private_schema_or_raw_materialized_views(
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario_id = await novo_usuario()
    async with como(engine_app, usuario_id) as session:
        has_usage = await session.scalar(
            text("SELECT has_schema_privilege(current_user, 'painel', 'USAGE')")
        )
        assert has_usage is False
        with pytest.raises(DBAPIError) as error:
            await session.execute(text("SELECT * FROM painel.mv_painel_resumo"))

    assert error.value.orig.sqlstate == "42501"


async def test_materialized_metrics_match_the_canonical_financial_rules(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Como,
    novo_usuario: Callable[[], Awaitable[int]],
) -> None:
    usuario_id = await novo_usuario()
    bets = (
        # Normal win: +9,000 over a 10,000 ROI base.
        {"estado": "GREEN", "stake_centavos": 10_000, "retorno_centavos": 19_000},
        # Normal loss: -10,000 over a 10,000 ROI base.
        {"estado": "RED", "stake_centavos": 10_000, "retorno_centavos": 0},
        # Freebet: zero stake, but its 10,000 face value is the ROI denominator.
        {
            "estado": "GREEN",
            "stake_centavos": 0,
            "valor_aposta_centavos": 10_000,
            "retorno_centavos": 9_000,
            "freebet": True,
        },
        # Pending and annulled bets count, but never enter turnover or profit.
        {"estado": "PENDENTE", "stake_centavos": 5_000, "retorno_centavos": None},
        {"estado": "ANULADA", "stake_centavos": 7_000, "retorno_centavos": 7_000},
        # A retained settled row without a measured payout follows resumir(): turnover and
        # ROI base count, but an unknown payout is not silently converted into a full loss.
        {"estado": "CASHOUT", "stake_centavos": 4_000, "retorno_centavos": None},
        # Neither a grave review nor a soft-deleted bet is a dashboard fact.
        {
            "estado": "GREEN",
            "stake_centavos": 100_000,
            "retorno_centavos": 200_000,
            "revisao_grave": True,
        },
        {
            "estado": "RED",
            "stake_centavos": 100_000,
            "retorno_centavos": 0,
            "selecionada": False,
        },
    )
    for bet in bets:
        await _insert_bet(engine_app, como, usuario_id, **bet)
    await _refresh(engine_admin)

    async with como(engine_app, usuario_id) as session:
        summary = (
            (await session.execute(text("SELECT * FROM public.painel_resumo"))).mappings().one()
        )
        period_rows = await session.scalar(text("SELECT count(*) FROM public.painel_por_periodo"))

    assert summary["total_apostas"] == 6
    assert summary["pendentes"] == 1
    assert (summary["greens"], summary["reds"]) == (2, 1)
    assert summary["giro_centavos"] == Decimal(24_000)
    assert summary["base_roi_centavos"] == Decimal(34_000)
    assert summary["retorno_centavos"] == Decimal(28_000)
    assert summary["lucro_centavos"] == Decimal(8_000)
    assert summary["freebets"] == 1
    assert abs(summary["roi"] - Decimal(8_000) / Decimal(34_000)) < Decimal("1e-18")
    assert abs(summary["win_rate"] - Decimal(2) / Decimal(3)) < Decimal("1e-18")
    assert summary["saldo_centavos"] == Decimal(0)
    assert summary["saldo_conhecido_centavos"] == 0
    assert summary["saldo_conhecido"] is True
    assert period_rows == 3


def _migration_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


async def _schema_exists(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return bool(
                await connection.scalar(text("SELECT 1 FROM pg_namespace WHERE nspname = 'painel'"))
            )
    finally:
        await engine.dispose()


async def _restore_app_grants(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            await connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
                )
            )
    finally:
        await engine.dispose()


def test_revision_008_downgrades_and_reupgrades_without_losing_application_access(banco) -> None:
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = banco.url_admin
    try:
        command.downgrade(_migration_config(banco.url_admin), PREVIOUS)
        assert asyncio.run(_schema_exists(banco.url_admin)) is False
    finally:
        command.upgrade(_migration_config(banco.url_admin), "head")
        asyncio.run(_restore_app_grants(banco.url_admin))
        if previous_database_url is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous_database_url

    assert asyncio.run(_schema_exists(banco.url_admin)) is True
