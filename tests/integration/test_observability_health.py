from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia.db.session import connect_args
from bancaemdia.observability.health import check_postgres_primary, check_postgres_replica

pytestmark = pytest.mark.xdist_group("postgres")


async def test_postgres_readiness_probes_real_primary_and_read_only_pool(banco, engine_app) -> None:
    replica_engine = create_async_engine(
        banco.url_app,
        poolclass=NullPool,
        connect_args=connect_args(replica=True),
    )
    try:
        primary = await check_postgres_primary(engine_app)
        replica = await check_postgres_replica(replica_engine)
    finally:
        await replica_engine.dispose()

    assert primary == {"mode": "writable"}
    assert replica == {"mode": "read_only", "in_recovery": False}
