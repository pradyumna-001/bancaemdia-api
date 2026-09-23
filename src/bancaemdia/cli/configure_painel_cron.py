"""Install the owner-run 15-second dashboard refresh job on the primary database."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from bancaemdia.cli.refresh_painel import MATERIALIZED_VIEWS, PAINEL_REFRESH_LOCK
from bancaemdia.db.session import engine

JOB = "bancaemdia_painel_refresh"
INTERVAL = "15 seconds"
COMMAND = "\n".join((
    "BEGIN;",
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;",
    "SET LOCAL statement_timeout = 0;",
    f"SELECT pg_advisory_xact_lock({PAINEL_REFRESH_LOCK});",
    *(f"REFRESH MATERIALIZED VIEW CONCURRENTLY painel.{name};" for name in MATERIALIZED_VIEWS),
    "UPDATE painel.estado_refresh SET atualizado_em = clock_timestamp() WHERE id = 1;",
    "COMMIT;",
))


async def configure() -> int:
    async with engine.begin() as conn:
        if await conn.scalar(text("SELECT pg_is_in_recovery()")):
            raise RuntimeError("the refresh job must run on the primary")
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_cron"))
        job_id = await conn.scalar(
            text(
                "SELECT jobid FROM cron.job WHERE jobname = :job AND database = current_database()"
            ),
            {"job": JOB},
        )
        if job_id is None:
            job_id = await conn.scalar(
                text("SELECT cron.schedule(:job, :interval, :command)"),
                {"job": JOB, "interval": INTERVAL, "command": COMMAND},
            )
        else:
            await conn.execute(
                text("SELECT cron.alter_job(:job_id, :interval, :command)"),
                {"job_id": job_id, "interval": INTERVAL, "command": COMMAND},
            )
        return int(job_id)


if __name__ == "__main__":
    asyncio.run(configure())
