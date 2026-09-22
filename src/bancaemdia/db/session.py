import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id, use_primary

settings = get_settings()

LAG_WARNING_SECONDS = 30
LAG_CHECK_SECONDS = 30
# Com o primário parado, a hora da última transação reaplicada não anda e a subtração vira atraso falso
# (medido). Recebido = reaplicado só é zero com o receptor de WAL ligado: desligado, os dois ficam iguais e
# parados enquanto a cópia envelhece (medido); fora de recuperação a função fica parada (docs PG 16).
REPLICA_LAG_SQL = text(
    "SELECT CASE"
    " WHEN NOT pg_is_in_recovery() THEN NULL"
    " WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()"
    " AND EXISTS (SELECT 1 FROM pg_stat_wal_receiver) THEN 0"
    " ELSE EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())"
    " END"
)


def connect_args(replica: bool) -> dict[str, object]:
    if not replica:
        return {"server_settings": {"statement_timeout": str(settings.STATEMENT_TIMEOUT_PRIMARY)}}
    # A cópia recusa escrita no próprio servidor mesmo quando aponta para o mesmo banco do primário
    # (dev, CI): uma leitura que escreve quebra nos testes, não só em produção.
    return {
        "server_settings": {
            "statement_timeout": str(settings.STATEMENT_TIMEOUT_REPLICA),
            "default_transaction_read_only": "on",
        }
    }


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=connect_args(replica=False),
)

replica_engine = create_async_engine(
    settings.DATABASE_URL_REPLICA,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=connect_args(replica=True),
)


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
ReplicaSession = async_sessionmaker(replica_engine, expire_on_commit=False, class_=AsyncSession)


async def replica_lag_seconds(conn: AsyncConnection) -> float | None:
    seconds = await conn.scalar(REPLICA_LAG_SQL)
    return None if seconds is None else float(seconds)


class ReplicaLagCheck:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.next_at: float | None = None

    async def run(self, session: AsyncSession) -> None:
        now = self.clock()
        bind = session.bind
        if (self.next_at is not None and now < self.next_at) or not isinstance(bind, AsyncEngine):
            return
        self.next_at = now + LAG_CHECK_SECONDS
        # Uma falha aqui não pode virar erro do pedido: ao conectar, o asyncpg entrega os erros crus
        # (ConnectionRefusedError, TooManyConnectionsError), fora dos erros do SQLAlchemy (medido).
        try:
            async with bind.connect() as conn:
                seconds = await replica_lag_seconds(conn)
        except Exception as error:
            structlog.get_logger().warning("replica_lag_unavailable", error=type(error).__name__)
            return
        if seconds is not None and seconds > LAG_WARNING_SECONDS:
            structlog.get_logger().warning("replica_lag", seconds=round(seconds, 1))


replica_lag = ReplicaLagCheck()


@asynccontextmanager
async def _open(replica: bool) -> AsyncIterator[AsyncSession]:
    async with (ReplicaSession if replica else SessionLocal)() as session:
        # Antes de a sessão pegar a sua conexão: a checagem devolve a dela ao pool, e um pedido nunca
        # segura duas conexões da cópia.
        if replica:
            await replica_lag.run(session)
        user_id = current_user_id.get()
        if user_id:
            await session.execute(
                text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": user_id}
            )
        yield session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _open(replica=not use_primary.get()) as session:
        yield session


async def get_db_primary() -> AsyncGenerator[AsyncSession, None]:
    async with _open(replica=False) as session:
        yield session


async def get_db_replica() -> AsyncGenerator[AsyncSession, None]:
    async with _open(replica=True) as session:
        yield session


async def get_read_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def check_db_health() -> bool:
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
