from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost:5432/bancaemdia",
)
os.environ.setdefault(
    "DATABASE_URL_REPLICA", "postgresql+asyncpg://user:password@localhost:5432/bancaemdia"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "TEST_SECRET_KEY_REPLACE_IN_PRODUCTION")
os.environ.setdefault("JWT_SECRET_KEY", "TEST_JWT_SECRET")
os.environ.setdefault("COLETA_TOKEN_SECRET", "TEST_COLETA_TOKEN_SECRET")
os.environ.setdefault("UPLOAD_WEBHOOK_SECRET", "TEST_UPLOAD_WEBHOOK_SECRET")
os.environ.setdefault("API_INTERNAL_URL", "http://api.test")
os.environ.setdefault("JWT_AUDIENCE", "test")
os.environ.setdefault("JWT_ISSUER", "test")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "INFO")
# Tests reset buckets between cases and must never point that reset at a shared Redis instance.
os.environ["RATE_LIMIT_STORAGE"] = "memory://"
os.environ.setdefault("STATEMENT_TIMEOUT_PRIMARY", "5000")
os.environ.setdefault("STATEMENT_TIMEOUT_REPLICA", "30000")


@pytest.fixture(autouse=True)
def _isolate_api_rate_limit_buckets():
    # Production buckets deliberately outlive requests. Tests must not depend on execution order
    # or on which xdist worker happened to run a previous request with the same user.
    from bancaemdia.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()
    yield
    reset_rate_limiters()


ROOT = Path(__file__).resolve().parents[1]
PAPEL = "bancaemdia_app"
SENHA = "bancaemdia_app"
IMAGEM = "postgres:16"


@dataclass(frozen=True)
class Banco:
    url_admin: str
    url_app: str


def _em_outra_thread(tarefa: Callable[[], Any]) -> Any:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(tarefa).result()


def _migrar(url: str) -> None:
    anterior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        _em_outra_thread(lambda: command.upgrade(config, "head"))
    finally:
        if anterior is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = anterior


async def _preparar_papel(url_admin: str) -> None:
    engine = create_async_engine(url_admin)
    try:
        async with engine.begin() as conn:
            existe = await conn.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :papel"), {"papel": PAPEL}
            )
            if not existe:
                await conn.execute(text(f"CREATE ROLE {PAPEL} LOGIN PASSWORD '{SENHA}'"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {PAPEL}"))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {PAPEL}"
                )
            )
            await conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {PAPEL}")
            )
            await conn.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION telegram_link_code_issuer(text), "
                    f"telegram_link_active_owner(bigint,bigint), telegram_link_attempt_state(text), "
                    f"telegram_link_record_failure(text,text), telegram_link_clear_attempts(text) TO {PAPEL}"
                )
            )
    finally:
        await engine.dispose()


def _url_do_papel(url_admin: str) -> str:
    return (
        make_url(url_admin)
        .set(username=PAPEL, password=SENHA)
        .render_as_string(hide_password=False)
    )


async def _preparar_com_trava(url_admin: str) -> None:
    engine = create_async_engine(url_admin)
    try:
        async with engine.connect() as trava:
            await trava.execute(text("SELECT pg_advisory_lock(20260905)"))
            try:
                await asyncio.to_thread(_migrar, url_admin)
                await _preparar_papel(url_admin)
            finally:
                await trava.execute(text("SELECT pg_advisory_unlock(20260905)"))
    finally:
        await engine.dispose()


def _preparar(url_admin: str) -> Banco:
    _em_outra_thread(lambda: asyncio.run(_preparar_com_trava(url_admin)))
    return Banco(url_admin=url_admin, url_app=_url_do_papel(url_admin))


def _docker_disponivel() -> bool:
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def banco() -> Iterator[Banco]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield _preparar(url)
        return
    if not _docker_disponivel():
        pytest.skip("integration tests need Docker or TEST_DATABASE_URL pointing at a PostgreSQL")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(IMAGEM, driver="asyncpg") as container:
        yield _preparar(container.get_connection_url())


@pytest.fixture
async def engine_admin(banco: Banco) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(banco.url_admin)
    yield engine
    await engine.dispose()


@pytest.fixture
async def engine_app(banco: Banco) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(banco.url_app)
    yield engine
    await engine.dispose()


Como = Callable[[AsyncEngine, int | None], AbstractAsyncContextManager[AsyncSession]]


@pytest.fixture
def como() -> Como:
    @asynccontextmanager
    async def _como(engine: AsyncEngine, usuario_id: int | None) -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            if usuario_id is not None:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(usuario_id)},
                )
            yield session

    return _como


@pytest.fixture
def novo_usuario(engine_app: AsyncEngine, como: Como) -> Callable[[], Awaitable[int]]:
    from bancaemdia.repositories.usuario_repo import UsuarioRepo

    async def _criar() -> int:
        async with como(engine_app, None) as session:
            usuario = await UsuarioRepo().create(
                session, f"{uuid4().hex[:10]}@teste.local", "Teste"
            )
            await session.commit()
            return usuario.id

    return _criar
