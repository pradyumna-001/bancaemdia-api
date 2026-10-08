"""Local prelaunch evidence with a production-sized, entirely synthetic tenant."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bancaemdia import main
from bancaemdia.auth import middleware as auth_middleware
from bancaemdia.auth.jwt import JWKSCache, jwt
from bancaemdia.cli.refresh_painel import refresh_painel
from bancaemdia.cli.replay import reconstruir_usuario
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.seed import seed_canonical
from bancaemdia.db.session import get_db_snapshot
from bancaemdia.middleware.rate_limit import api_limiter

pytestmark = pytest.mark.xdist_group("postgres")
TOTAL_BETS = 16_000


@pytest.fixture
async def engine_admin(banco_migracao) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(banco_migracao.url_admin)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def engine_app(banco_migracao) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(banco_migracao.url_app)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _ids(engine: AsyncEngine, usuario_id: int) -> tuple[list[int], list[int], int, int]:
    async with engine.begin() as conn:
        await seed_canonical(conn)
        house_ids = list(
            (await conn.execute(text("SELECT id FROM casas ORDER BY id LIMIT 5"))).scalars()
        )
        mercado = await conn.scalar(text("SELECT id FROM mercados ORDER BY id LIMIT 1"))
        tipster = await conn.scalar(
            text("INSERT INTO tipsters (nome) VALUES (:nome) RETURNING id"),
            {"nome": f"Prelaunch {uuid4().hex}"},
        )
        bancas = []
        contas = []
        for indice, house_id in enumerate(house_ids):
            banca = await conn.scalar(
                text(
                    "INSERT INTO bancas (usuario_id, nome, saldo_inicial_centavos) "
                    "VALUES (:uid, :nome, 100000) RETURNING id"
                ),
                {"uid": usuario_id, "nome": f"Prelaunch {indice} {uuid4().hex}"},
            )
            conta = await conn.scalar(
                text(
                    "INSERT INTO contas_casa (usuario_id, casa_id, banca_id, apelido) "
                    "VALUES (:uid, :casa, :banca, :apelido) RETURNING id"
                ),
                {"uid": usuario_id, "casa": house_id, "banca": banca, "apelido": "test"},
            )
            bancas.append(banca)
            contas.append(conta)
    assert len(bancas) == len(contas) == 5
    assert isinstance(mercado, int) and isinstance(tipster, int)
    return bancas, contas, mercado, tipster


def _choice(column: str) -> str:
    return (
        "CASE g % 5 "
        + " ".join(f"WHEN {index} THEN CAST(:{column}{index} AS bigint)" for index in range(5))
        + " END"
    )


async def _seed(engine: AsyncEngine, usuario_id: int) -> tuple[str, int, datetime]:
    bancas, contas, mercado, tipster = await _ids(engine, usuario_id)
    prefix = uuid4().hex
    base = datetime.now(UTC).replace(microsecond=0)
    values = {
        "uid": usuario_id,
        "prefix": prefix,
        "base": base,
        "mercado": mercado,
        "tipster": tipster,
        **{f"banca{i}": value for i, value in enumerate(bancas)},
        **{f"conta{i}": value for i, value in enumerate(contas)},
    }
    key = "'prelaunch-' || :prefix || '-' || g::text"
    data = "CAST(:base AS timestamptz) - make_interval(days => g % 28)"
    state = (
        "CASE g % 4 WHEN 0 THEN 'GREEN' WHEN 1 THEN 'RED' WHEN 2 THEN 'PENDENTE' ELSE 'ANULADA' END"
    )
    return_value = "CASE g % 4 WHEN 0 THEN 20000 WHEN 1 THEN 0 WHEN 2 THEN NULL ELSE 10000 END"
    selected = "g % 20 <> 0"
    severe = "g % 25 = 0"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO apostas (usuario_id, chave, origem, banca_id, conta_casa_id, "
                "tipster_id, mercado_id, data_aposta, data_jogo, stake_unidades, stake_centavos, "
                "valor_aposta_centavos, odd, retorno_centavos, estado, freebet, "
                "selecionada, revisao_grave) "
                f"SELECT :uid, {key}, 'manual', {_choice('banca')}, {_choice('conta')}, "
                f":tipster, :mercado, {data}, {data}, 1, 10000, 10000, 2.0, "
                f"{return_value}, {state}, false, {selected}, {severe} "
                f"FROM generate_series(1, {TOTAL_BETS}) AS g"
            ),
            values,
        )
        await conn.execute(
            text(
                "INSERT INTO eventos (usuario_id, tipo, fonte, payload_json, aposta_chave) "
                "SELECT :uid, 'APOSTA_CRIADA', 'manual', jsonb_build_object("
                "'origem', 'manual', 'stake_unidades', 1, 'valor_unidade_centavos', 10000, "
                "'odd', 2.0, 'casa', (SELECT casa.nome FROM contas_casa c JOIN casas casa ON casa.id=c.casa_id "
                f"WHERE c.id={_choice('conta')}), 'data_aposta', {data}, 'data_jogo', {data}, "
                f"'conta_casa_id', {_choice('conta')}, "
                f"'conta_casa_ref', {_choice('conta')}, 'conta_referencia_explicita', true, "
                "'tipster_id', CAST(:tipster AS bigint), "
                "'mercado_id', CAST(:mercado AS bigint), "
                f"'selecionada', {selected}, 'revisao_grave', {severe}), {key} "
                f"FROM generate_series(1, {TOTAL_BETS}) AS g"
            ),
            values,
        )
        await conn.execute(
            text(
                "INSERT INTO eventos (usuario_id, tipo, fonte, payload_json, aposta_chave) "
                "SELECT :uid, 'RESULTADO_REGISTRADO', 'manual', "
                f"jsonb_build_object('estado', {state}, 'retorno_centavos', "
                f"{return_value}), {key} "
                f"FROM generate_series(1, {TOTAL_BETS}) AS g WHERE g % 4 <> 2"
            ),
            values,
        )
        published = await conn.scalar(
            text(
                "SELECT count(*) FROM apostas WHERE usuario_id = :uid "
                "AND selecionada AND NOT revisao_grave"
            ),
            {"uid": usuario_id},
        )
    assert isinstance(published, int)
    return prefix, published, base


async def _serve() -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    config = uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            return server, task, port
        if task.done():
            raise RuntimeError("local HTTP server stopped before startup")
        await asyncio.sleep(0.05)
    server.should_exit = True
    await task
    raise RuntimeError("local HTTP server did not start")


async def test_synthetic_painel_and_replay_at_16k_bets(
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    novo_usuario: Callable[[], Awaitable[int]],
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    usuario_id = await novo_usuario()
    prefix, published, base = await _seed(engine_admin, usuario_id)
    # This benchmark requires a quiescent database. Its private migrated database
    # excludes other tests' writes, and completes real maintenance after the bulk
    # seed. Replay still refuses writers/maintenance with NOWAIT; dedicated tests
    # assert that refusal and absence of partial updates for every protected table.
    async with engine_admin.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text("VACUUM (ANALYZE) eventos, apostas, movimentos, aposta_consolidacoes")
        )
    record_property("synthetic_bets", TOTAL_BETS)
    record_property("synthetic_bancas", 5)
    record_property("published_bets", published)

    first = await reconstruir_usuario(usuario_id, engine=engine_app, dry_run=True)
    assert (first.apostas, first.alteradas, first.recriadas) == (TOTAL_BETS, 0, 0)
    record_property("replay_events", first.eventos)

    async with engine_admin.begin() as conn:
        key = f"prelaunch-{prefix}-1"
        original_id = await conn.scalar(
            text("SELECT id FROM apostas WHERE usuario_id = :uid AND chave = :key"),
            {"uid": usuario_id, "key": key},
        )
        await conn.execute(
            text("UPDATE apostas SET stake_centavos = 1 WHERE usuario_id = :uid AND chave = :key"),
            {"uid": usuario_id, "key": key},
        )
    selected_day = base - timedelta(days=1)
    repaired = await reconstruir_usuario(
        usuario_id,
        selected_day,
        selected_day + timedelta(seconds=1),
        engine=engine_app,
    )
    assert repaired.alteradas == 1
    async with engine_admin.connect() as conn:
        id_and_stake = (
            await conn.execute(
                text(
                    "SELECT id, stake_centavos FROM apostas WHERE usuario_id = :uid AND chave = :key"
                ),
                {"uid": usuario_id, "key": key},
            )
        ).one()
    assert id_and_stake == (original_id, 10000)
    final = await reconstruir_usuario(usuario_id, engine=engine_app, dry_run=True)
    assert (final.apostas, final.alteradas, final.recriadas) == (TOTAL_BETS, 0, 0)

    await refresh_painel(engine_admin)
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = signing.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    numbers = signing.public_key().public_numbers()

    def jwk_integer(value: int) -> str:
        return (
            base64
            .urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))
            .rstrip(b"=")
            .decode()
        )

    cache = JWKSCache(None, "RS256")
    cache.keys = {
        "local-test": {
            "kty": "RSA",
            "n": jwk_integer(numbers.n),
            "e": jwk_integer(numbers.e),
            "alg": "RS256",
            "use": "sig",
            "kid": "local-test",
        }
    }
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(auth_middleware, "get_jwks_cache", lambda: cache)
    # A single synthetic user makes 221 requests in seconds; this benchmark isolates the
    # dashboard's latency rather than the separately tested request limiter.
    monkeypatch.setattr(api_limiter, "enabled", False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine_app) as session:
            user = current_user_id.get()
            if user:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(user)},
                )
            yield session

    main.app.dependency_overrides[get_db_snapshot] = session_override
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": str(usuario_id),
            "aud": settings.JWT_AUDIENCE,
            "iss": settings.JWT_ISSUER,
            "exp": int(time.time()) + 600,
        },
        private,
        "RS256",
        headers={"kid": "local-test"},
    )
    server, task, port = await _serve()
    try:
        async with httpx.AsyncClient() as client:
            probe = await client.get(
                f"http://127.0.0.1:{port}/api/v1/painel?periodo=30d",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert probe.status_code == 200, probe.text
        assert probe.json()["resumo"]["total_apostas"] == published
        env = {
            **os.environ,
            "PAINEL_BENCH_URL": f"http://127.0.0.1:{port}/api/v1/painel?periodo=30d",
            "PAINEL_BENCH_TOKEN": token,
            "PAINEL_BENCH_MIN_BETS": str(published),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts" / "benchmark_painel.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await process.communicate()
        assert stdout, stderr.decode(errors="replace")
        result = json.loads(stdout)
        record_property("panel_p95_ms", result["p95_ms"])
        record_property("panel_max_age_seconds", result["max_idade_mv_segundos"])
        assert process.returncode == 0, result
        assert result["passou"]
    finally:
        server.should_exit = True
        await task
        main.app.dependency_overrides.pop(get_db_snapshot, None)
