"""A fixed query shape must still observe live commercial access on every call."""

import os

import pytest
from sqlalchemy import event, text

from bancaemdia.repositories.assinatura_repo import AssinaturaRepo

pytestmark = pytest.mark.xdist_group("postgres")


@pytest.fixture(scope="session", autouse=True)
def require_database():
    if not os.environ.get("TEST_DATABASE_URL"):
        from testcontainers.core.docker_client import DockerClient

        assert DockerClient().client.ping(), "runtime acceptance requires PostgreSQL"


async def test_one_billing_query_observes_live_rollout_without_permission_cache(
    engine_admin, engine_app, como, novo_usuario
):
    uid = await novo_usuario()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    async with engine_admin.begin() as conn:
        previous = await conn.scalar(text("SELECT activated_at FROM billing_rollout WHERE id=1"))
        await conn.execute(text("UPDATE billing_rollout SET activated_at=NULL WHERE id=1"))
    try:
        async with como(engine_app, uid) as session:
            event.listen(engine_app.sync_engine, "before_cursor_execute", capture)
            try:
                assert (
                    await AssinaturaRepo().read_status(session, uid)
                ).access.value == "FULL_WRITE"
                assert len(statements) == 1
                async with engine_admin.begin() as conn:
                    await conn.execute(
                        text("UPDATE billing_rollout SET activated_at=now() WHERE id=1")
                    )
                statements.clear()
                assert (
                    await AssinaturaRepo().read_status(session, uid)
                ).access.value == "READ_ONLY"
                assert len(statements) == 1
            finally:
                event.remove(engine_app.sync_engine, "before_cursor_execute", capture)
    finally:
        async with engine_admin.begin() as conn:
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=:p WHERE id=1"), {"p": previous}
            )
