from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "TEST_SECRET_KEY_REPLACE_IN_PRODUCTION")
os.environ.setdefault("JWT_SECRET_KEY", "TEST_JWT_SECRET")
os.environ.setdefault("JWT_AUDIENCE", "test")
os.environ.setdefault("JWT_ISSUER", "test")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("RATE_LIMIT_STORAGE", "memory://")
os.environ.setdefault("STATEMENT_TIMEOUT_PRIMARY", "5000")
os.environ.setdefault("STATEMENT_TIMEOUT_REPLICA", "30000")

import bancaemdia.db.session as db_session


def _is_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _ci_db_urls() -> tuple[str, str]:
    """Return (async_url, sync_url) for CI postgres service."""
    async_url = os.environ.get("DATABASE_URL")
    if not async_url:
        pw = os.environ.get("POSTGRES_PASSWORD", "test_password")
        async_url = f"postgresql+asyncpg://bancaemdia:{pw}@localhost:5432/bancaemdia"
    sync_url = async_url.replace("postgresql+asyncpg", "postgresql", 1)
    return async_url, sync_url


@pytest.fixture(scope="session")
def migrated_db_url() -> Generator[str, None, None]:
    """Provide a migrated database URL, using CI service or testcontainers."""
    if _is_ci():
        async_url, _ = _ci_db_urls()
        original_db_url = os.environ.get("DATABASE_URL")
        original_migration_url = os.environ.get("MIGRATION_DATABASE_URL")
        os.environ["DATABASE_URL"] = async_url
        os.environ["MIGRATION_DATABASE_URL"] = async_url
        try:
            subprocess.run(
                ["alembic", "upgrade", "heads"],
                check=True,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            yield async_url
        finally:
            if original_db_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = original_db_url
            if original_migration_url is None:
                os.environ.pop("MIGRATION_DATABASE_URL", None)
            else:
                os.environ["MIGRATION_DATABASE_URL"] = original_migration_url
    else:
        from docker.errors import DockerException
        from testcontainers.postgres import PostgresContainer

        try:
            with PostgresContainer("postgres:16") as pg:
                url = pg.get_connection_url(driver="asyncpg")
                original_db_url = os.environ.get("DATABASE_URL")
                original_migration_url = os.environ.get("MIGRATION_DATABASE_URL")
                os.environ["DATABASE_URL"] = url
                os.environ["MIGRATION_DATABASE_URL"] = url
                try:
                    subprocess.run(
                        ["alembic", "upgrade", "heads"],
                        check=True,
                        cwd=str(Path(__file__).resolve().parent.parent),
                    )
                    yield url
                finally:
                    if original_db_url is None:
                        os.environ.pop("DATABASE_URL", None)
                    else:
                        os.environ["DATABASE_URL"] = original_db_url
                    if original_migration_url is None:
                        os.environ.pop("MIGRATION_DATABASE_URL", None)
                    else:
                        os.environ["MIGRATION_DATABASE_URL"] = original_migration_url
        except DockerException:
            pytest.skip(
                "Docker daemon not reachable. Start Docker Desktop or configure DOCKER_HOST."
            )


@pytest.fixture(scope="session")
def bancaemdia_app_role(migrated_db_url: str) -> Generator[str, None, None]:
    admin_url = migrated_db_url.replace("postgresql+asyncpg", "postgresql", 1)

    async def _admin() -> asyncpg.Connection:
        return await asyncpg.connect(admin_url)

    async def _setup() -> None:
        conn = await _admin()
        try:
            try:
                await conn.execute("ALTER ROLE postgres WITH PASSWORD 'postgres'")
            except asyncpg.PostgresError:
                pass
            await conn.execute(
                "CREATE ROLE bancaemdia_app WITH LOGIN PASSWORD 'bancaemdia_app_pwd'"
            )
            await conn.execute("GRANT USAGE ON SCHEMA public TO bancaemdia_app")
            await conn.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bancaemdia_app"
            )
            await conn.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bancaemdia_app"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS test_tenant_data (
                    id SERIAL PRIMARY KEY,
                    usuario_id INT NOT NULL,
                    data TEXT NOT NULL
                )
                """
            )
            await conn.execute("ALTER TABLE test_tenant_data ENABLE ROW LEVEL SECURITY")
            await conn.execute(
                """
                CREATE POLICY test_tenant_isolation ON test_tenant_data
                USING (usuario_id = current_setting('app.current_user_id')::int)
                """
            )
            await conn.execute(
                """
                INSERT INTO test_tenant_data (usuario_id, data) VALUES
                (1, 'user1-data1'),
                (1, 'user1-data2'),
                (2, 'user2-data1'),
                (3, 'user3-data1')
                """
            )
        finally:
            await conn.close()

    async def _teardown() -> None:
        conn = await _admin()
        try:
            await conn.execute("DROP TABLE IF EXISTS test_tenant_data")
            await conn.execute("DROP ROLE IF EXISTS bancaemdia_app")
        finally:
            await conn.close()

    asyncio.run(_setup())
    try:
        yield f"postgresql+asyncpg://bancaemdia_app:bancaemdia_app_pwd@{admin_url.split('@', 1)[1]}"
    finally:
        asyncio.run(_teardown())


@pytest.fixture
def bound_engine(migrated_db_url: str) -> Generator[None, None, None]:
    engine = create_async_engine(migrated_db_url, echo=False)
    db_session.engine = engine
    db_session.SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    yield
    engine.sync_engine.dispose()
