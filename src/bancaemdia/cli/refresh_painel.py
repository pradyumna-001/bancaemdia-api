"""Atualiza o conjunto do painel no primário sem bloquear as leituras."""

from __future__ import annotations

import asyncio
from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from bancaemdia.db.session import engine

MATERIALIZED_VIEWS = (
    "mv_painel_resumo",
    "mv_painel_por_casa",
    "mv_painel_por_tipster",
    "mv_painel_por_mercado",
    "mv_painel_por_periodo",
    "mv_painel_evolucao_banca",
)

# O mesmo número aparece no job opcional de pg_cron criado pela migração 008. A trava é de
# sessão para também coordenar invocações externas; ela é sempre liberada no finally (e pelo
# PostgreSQL automaticamente se a conexão cair).
PAINEL_REFRESH_LOCK = 2_026_09_30


class RefreshPainelEmAndamentoError(RuntimeError):
    """Outro mantenedor já está atualizando as materialized views."""


async def _adquirir_trava(conn: AsyncConnection) -> bool:
    adquiriu = await conn.scalar(
        text("SELECT pg_try_advisory_lock(:lock_id)"),
        {"lock_id": PAINEL_REFRESH_LOCK},
    )
    # A advisory lock é da sessão, não da transação. Encerrar esta transação curta permite abrir
    # abaixo um único snapshot para as seis materializações.
    await conn.commit()
    return bool(adquiriu)


async def _liberar_trava(conn: AsyncConnection) -> None:
    if conn.in_transaction():
        await conn.rollback()
    await conn.execute(
        text("SELECT pg_advisory_unlock(:lock_id)"),
        {"lock_id": PAINEL_REFRESH_LOCK},
    )
    await conn.commit()


async def refresh_painel(db_engine: AsyncEngine = engine) -> datetime:
    """Atualiza todas as MVs em um snapshot e só então avança o relógio público.

    O chamador precisa usar a credencial proprietária das MVs no primário. A credencial HTTP
    enxerga apenas as views tenant-safe do schema ``public`` e não pode executar este caminho.
    """

    async with db_engine.connect() as conn:
        if not await _adquirir_trava(conn):
            raise RefreshPainelEmAndamentoError("o painel já está sendo atualizado")
        try:
            async with conn.begin():
                # Refresh pode legitimamente levar mais que o timeout curto das transações OLTP.
                # Um só snapshot impede que cada dimensão represente um instante diferente.
                await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                await conn.execute(text("SET LOCAL statement_timeout = 0"))
                estado_existe = await conn.scalar(
                    text("SELECT id FROM painel.estado_refresh WHERE id = 1 FOR UPDATE")
                )
                if estado_existe != 1:
                    raise RuntimeError("o estado de atualização do painel não existe")
                for nome in MATERIALIZED_VIEWS:
                    await conn.execute(
                        text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY painel.{nome}")
                    )
                atualizado_em = await conn.scalar(
                    text(
                        "UPDATE painel.estado_refresh "
                        "SET atualizado_em = clock_timestamp() WHERE id = 1 "
                        "RETURNING atualizado_em"
                    )
                )
            if not isinstance(atualizado_em, datetime):
                raise RuntimeError("o horário de atualização do painel não foi gravado")
            return atualizado_em
        finally:
            await _liberar_trava(conn)


async def _main() -> None:
    atualizado_em = await refresh_painel()
    structlog.get_logger().info(
        "painel_materialized_views_refreshed",
        atualizado_em=atualizado_em.isoformat(),
        views=len(MATERIALIZED_VIEWS),
    )


if __name__ == "__main__":
    asyncio.run(_main())
