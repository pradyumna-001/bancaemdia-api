"""Disposable Phase 0 staging on a GitHub-hosted Linux runner, never a production seeder."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import os
import pstats
import re
import secrets
import signal
import ssl
import subprocess
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

import asyncpg
import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from jwt.algorithms import RSAAlgorithm

ROOT = Path(__file__).resolve().parents[1]
LABEL = "bancaemdia.k6-run"
USER_COUNT = 250
COLLECTION_COUNT = 100
WORKER_CONCURRENCY = {"extraction": 2, "materialization": 1}


def prepare_tls(directory: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509
        .CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    private = directory / "tls.key"
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    private.chmod(0o600)
    (directory / "tls.crt").write_bytes(certificate.public_bytes(Encoding.PEM))


def runtime_dir() -> Path:
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("This seeder requires an isolated GitHub Actions Linux runner")
    runner = Path(os.environ["RUNNER_TEMP"]).resolve()
    directory = Path(os.environ["K6_STAGING_DIR"]).resolve()
    if not directory.is_relative_to(runner) or directory == runner:
        raise RuntimeError("K6_STAGING_DIR must be a child of RUNNER_TEMP")
    if directory.is_relative_to(ROOT):
        raise RuntimeError("Runtime credentials must stay outside the checkout")
    return directory


def write_private(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def state(directory: Path) -> dict:
    return json.loads((directory / "state.json").read_text(encoding="utf-8"))


def process_cpu(info: dict) -> dict[str, float]:
    """Numeric CPU diagnostics for this run's live Python process groups only."""
    groups = dict(
        zip(
            info["processes"],
            ("issuer", "extraction", "materialization", "api", "refresh"),
            strict=False,
        )
    )
    totals = dict.fromkeys(groups.values(), 0.0)
    ticks = os.sysconf("SC_CLK_TCK")
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            name = groups.get(int(fields[2]))
            if name is not None:
                totals[name] += (int(fields[11]) + int(fields[12])) / ticks
        except (OSError, ValueError, IndexError):
            continue
    return totals


def cpu_since_ready(info: dict) -> dict[str, float]:
    baseline = info.get("cpu_baseline", {})
    return {
        name: round(max(0, value - baseline.get(name, 0)), 3)
        for name, value in process_cpu(info).items()
    }


def mask(value: str) -> None:
    # No workflow receives production credentials. Still mask all ephemeral credentials.
    sys.stdout.write(f"::add-mask::{value}\n")
    sys.stdout.flush()


def command(args: list[str], **kwargs: object) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)
    return result.stdout.strip()


async def connect(port: int, password: str, user: str = "k6_admin") -> asyncpg.Connection:
    return await asyncpg.connect(
        host="127.0.0.1",
        port=port,
        user=user,
        password=password,
        database="k6",
        timeout=3,
    )


async def wait_database(port: int, password: str, *, replica: bool = False) -> None:
    for _ in range(90):
        try:
            conn = await connect(port, password)
            try:
                recovering = await conn.fetchval("SELECT pg_is_in_recovery()")
                if recovering == replica:
                    return
            finally:
                await conn.close()
        except (OSError, asyncpg.PostgresError, TimeoutError):
            pass
        await asyncio.sleep(1)
    raise RuntimeError("PostgreSQL did not reach the required primary/standby state")


def docker_run(info: dict, service: str, args: list[str], env: dict | None = None) -> None:
    command(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            f"{info['name']}-{service}",
            "--label",
            f"{LABEL}={info['name']}",
            "--network",
            info["name"],
            *args,
        ],
        env=env,
    )


async def prepare(directory: Path) -> None:
    if directory.exists():
        raise RuntimeError("Refusing to reuse a previous staging runtime")
    directory.mkdir(mode=0o700, parents=True)
    prepare_tls(directory)
    # Temporary CPU measurement only in this disposable runner; no application edits.
    (directory / "k6_profiled_api.py").write_text(
        """import cProfile
import os
from pathlib import Path
import threading
import time

def create_app():
    from bancaemdia.main import app
    profile = cProfile.Profile(timer=time.process_time)
    profile.enable()
    destination = Path(os.environ["K6_STAGING_DIR"]) / f"api-{os.getpid()}.prof"
    def persist():
        while True:
            time.sleep(10)
            profile.dump_stats(str(destination))
    threading.Thread(target=persist, daemon=True).start()
    return app
""",
        encoding="utf-8",
    )
    info = {
        "name": f"k6-{secrets.token_hex(8)}",
        "admin_password": secrets.token_hex(32),
        "app_password": secrets.token_hex(32),
        "replica_password": secrets.token_hex(32),
        "collection_secret": secrets.token_hex(32),
        "webhook_secret": secrets.token_hex(32),
        "processes": [],
    }
    for key, value in info.items():
        if key.endswith(("password", "secret")):
            mask(value)
    write_private(directory / "state.json", info)
    command(["docker", "network", "create", "--label", f"{LABEL}={info['name']}", info["name"]])
    docker_run(
        info,
        "primary",
        [
            "--network-alias",
            "primary",
            "--publish",
            "127.0.0.1:15432:5432",
            "--env",
            "POSTGRES_PASSWORD",
            "--env",
            "POSTGRES_USER=k6_admin",
            "--env",
            "POSTGRES_DB=k6",
            "postgres:16",
            "postgres",
            "-c",
            "wal_level=replica",
            "-c",
            "max_wal_senders=5",
            "-c",
            "wal_keep_size=256MB",
            "-c",
            "max_connections=200",
            "-c",
            "shared_preload_libraries=pg_stat_statements",
            "-c",
            "pg_stat_statements.track=all",
        ],
        env={**os.environ, "POSTGRES_PASSWORD": info["admin_password"]},
    )
    docker_run(
        info,
        "redis",
        [
            "--publish",
            "127.0.0.1:16379:6379",
            "redis:7-alpine",
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        ],
    )
    await wait_database(15432, info["admin_password"])
    statistics = await connect(15432, info["admin_password"])
    try:
        await statistics.execute("CREATE EXTENSION pg_stat_statements")
    finally:
        await statistics.close()
    admin_url = f"postgresql+asyncpg://k6_admin:{info['admin_password']}@127.0.0.1:15432/k6"
    app_url = f"postgresql+asyncpg://k6_app:{info['app_password']}@127.0.0.1:15432/k6"
    env = {
        "DATABASE_URL": app_url,
        "DATABASE_URL_REPLICA": app_url.replace(":15432/", ":15433/"),
        # Retain the existing maximum of 30 connections rather than recreating overflow per burst.
        "DB_POOL_SIZE": "30",
        "DB_POOL_MAX_OVERFLOW": "0",
        "DB_POOL_PREWARM": "true",
        "DB_POOL_RECYCLE_SECONDS": "7200",
        "REDIS_URL": "redis://127.0.0.1:16379/2",
        "CELERY_BROKER_URL": "redis://127.0.0.1:16379/0",
        "CELERY_RESULT_BACKEND": "redis://127.0.0.1:16379/1",
        "RATE_LIMIT_STORAGE": "redis://127.0.0.1:16379/3",
        "COLETA_IP_RATE_LIMIT": "1200/minute",
        "COLETA_RATE_LIMIT": "10/minute",
        "COLETA_TOKEN_SECRET": info["collection_secret"],
        "UPLOAD_WEBHOOK_SECRET": info["webhook_secret"],
        "API_INTERNAL_URL": "https://127.0.0.1:18000",
        "SSL_CERT_FILE": str(directory / "tls.crt"),
        "JWT_SECRET_KEY": secrets.token_hex(32),
        "JWT_ALGORITHM": "RS256",
        "JWT_AUDIENCE": "k6-ephemeral",
        "JWT_ISSUER": "k6-ephemeral",
        "JWT_JWKS_URL": "http://127.0.0.1:18765/jwks.json",
        "APP_ENV": "staging",
        "LOG_LEVEL": "WARNING",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        # A cache miss must fail locally; it must never reach a paid AI provider.
        "ANTHROPIC_API_KEY": secrets.token_hex(32),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18999",
        "PYTHONPATH": str(directory) + os.pathsep + str(ROOT / "src"),
        "K6_STAGING_DIR": str(directory),
    }
    mask(admin_url)
    for key in ("DATABASE_URL", "DATABASE_URL_REPLICA", "JWT_SECRET_KEY", "ANTHROPIC_API_KEY"):
        mask(env[key])
    command(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, **env, "DATABASE_URL": admin_url},
    )
    conn = await connect(15432, info["admin_password"])
    try:
        # All interpolated passwords are generated hex, never supplied strings.
        await conn.execute(f"CREATE ROLE k6_app LOGIN PASSWORD '{info['app_password']}'")
        await conn.execute(
            f"CREATE ROLE k6_replica LOGIN REPLICATION PASSWORD '{info['replica_password']}'"
        )
        await conn.execute("GRANT USAGE ON SCHEMA public TO k6_app")
        await conn.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO k6_app"
        )
        await conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO k6_app")
        # This disposable role is created after migrations. The private Telegram
        # RLS helpers grant bancaemdia_app only when that role already exists.
        await conn.execute(
            "GRANT EXECUTE ON FUNCTION telegram_legacy_media_visible(text), "
            "telegram_chat_linked(bigint,bigint) TO k6_app"
        )
    finally:
        await conn.close()
    network = json.loads(command(["docker", "network", "inspect", info["name"]]))[0]
    subnet = network["IPAM"]["Config"][0]["Subnet"]
    hba = f"host replication k6_replica {subnet} scram-sha-256\n"
    command(
        [
            "docker",
            "exec",
            "--interactive",
            f"{info['name']}-primary",
            "sh",
            "-c",
            'cat >> "$PGDATA/pg_hba.conf"',
        ],
        input=hba,
    )
    conn = await connect(15432, info["admin_password"])
    try:
        await conn.execute("SELECT pg_reload_conf()")
    finally:
        await conn.close()
    await seed(directory, info, env)
    write_private(directory / "env.json", env)
    docker_run(
        info,
        "replica",
        [
            "--publish",
            "127.0.0.1:15433:5432",
            "--user",
            "postgres",
            "--env",
            "PGPASSWORD",
            "--tmpfs",
            "/var/lib/postgresql/data:uid=999,gid=999,mode=0700",
            "--entrypoint",
            "sh",
            "postgres:16",
            "-c",
            "pg_basebackup -h primary -U k6_replica -D /var/lib/postgresql/data -R -X stream "
            "&& exec postgres -D /var/lib/postgresql/data -c max_connections=200",
        ],
        env={**os.environ, "PGPASSWORD": info["replica_password"]},
    )
    await wait_database(15433, info["admin_password"], replica=True)


async def seed(directory: Path, info: dict, env: dict) -> None:
    # Both targets are newly created containers whose ownership is recorded above.
    from sqlalchemy.ext.asyncio import create_async_engine

    from bancaemdia.db.seed import seed_canonical

    url = f"postgresql+asyncpg://k6_admin:{info['admin_password']}@127.0.0.1:15432/k6"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await seed_canonical(connection)
    finally:
        await engine.dispose()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_id = secrets.token_hex(8)
    issuer = directory / "issuer"
    issuer.mkdir(mode=0o700)
    write_private(
        issuer / "jwks.json",
        {"keys": [{**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": key_id}]},
    )
    tokens = []
    collection_tokens = []
    conn = await connect(15432, info["admin_password"])
    try:
        casa = await conn.fetchval("SELECT id FROM casas WHERE nome = 'Betano'")
        if casa is None:
            raise RuntimeError("Canonical Betano seed not found")
        for index in range(USER_COUNT):
            uid = await conn.fetchval(
                "INSERT INTO usuarios(email,nome) VALUES($1,$2) RETURNING id",
                f"k6-{index}@example.invalid",
                f"Synthetic k6 {index}",
            )
            await conn.execute(
                "INSERT INTO unidades(usuario_id,valor_centavos,vigente_de) VALUES($1,1000,'2020-01-01')",
                uid,
            )
            banca = await conn.fetchval(
                "INSERT INTO bancas(usuario_id,nome,saldo_inicial_centavos) VALUES($1,'k6',1000000) RETURNING id",
                uid,
            )
            account = await conn.fetchval(
                "INSERT INTO contas_casa(usuario_id,casa_id,banca_id,apelido,desde) VALUES($1,$2,$3,'k6','2020-01-01') RETURNING id",
                uid,
                casa,
                banca,
            )
            await conn.execute(
                "INSERT INTO usos_conta_casa(usuario_id,casa_id,conta_casa_id,vigente_de) VALUES($1,$2,$3,'2020-01-01')",
                uid,
                casa,
                account,
            )
            tokens.append(
                jwt.encode(
                    {
                        "sub": str(uid),
                        "aud": env["JWT_AUDIENCE"],
                        "iss": env["JWT_ISSUER"],
                        "exp": int(time.time()) + 3 * 3600,
                    },
                    key,
                    algorithm="RS256",
                    headers={"kid": key_id},
                )
            )
            if index < COLLECTION_COUNT:
                token = secrets.token_urlsafe(32)
                digest = hmac.new(
                    info["collection_secret"].encode(), token.encode(), hashlib.sha256
                ).hexdigest()
                await conn.execute(
                    "INSERT INTO coleta_instalacoes(usuario_id,instalacao_publica_id,token_hash,pareado_em,expira_em) VALUES($1,$2,$3,now(),now()+interval '3 hours')",
                    uid,
                    uuid4(),
                    digest,
                )
                collection_tokens.append(token)
    finally:
        await conn.close()
    for token in [*tokens, *collection_tokens]:
        mask(token)
    write_private(directory / "tokens.json", {"jwt": tokens, "coleta": collection_tokens})
    os.environ.update(env)
    seed_extraction_cache(env)


def seed_extraction_cache(env: dict) -> None:
    # Only the existing, explicitly synthetic k6 archive is cached. No reader-review fixtures.
    from bancaemdia.cache.extracao_cache import chave_de_imagem, get_cache
    from bancaemdia.domain.upload import ExportTelegram
    from bancaemdia.extracao.cliente import Leitura
    from bancaemdia.extracao.modelos import ExtracaoBilhete, Selecao
    from bancaemdia.workers.materialization import FUSO_DO_BRASIL

    with (
        (ROOT / "k6/fixtures/telegram-small.zip").open("rb") as archive,
        ExportTelegram(archive, "telegram-small.zip") as export,
    ):
        message = next(export.mensagens())
        image = export.bytes_da_foto(message)
    if image is None:
        raise RuntimeError("Synthetic k6 export has no photo")
    coupon = ExtracaoBilhete(
        casa="Betano",
        evento="Flamengo x Palmeiras",
        odd_total=1.9,
        confianca=0.99,
        quando="2026-09-22T15:00:00",
        selecoes=[Selecao(mercado="Resultado Final", escolha="Flamengo", odd=1.9)],
    )
    # Upload stores a naive export clock as Brazilian local time, then the worker
    # converts its timestamptz back to that local minute for the extraction prompt.
    posted = message.data
    if posted.tzinfo is not None:
        posted = posted.astimezone(FUSO_DO_BRASIL).replace(tzinfo=None)
    cache_key = chave_de_imagem(image, message.texto, postada_em=posted)
    cache = get_cache()
    cache.guardar(
        cache_key, Leitura((coupon,), env.get("ANTHROPIC_ESCALATION_MODEL", "claude-sonnet-5"))
    )
    if cache.buscar(cache_key) is None:
        raise RuntimeError("Extraction cache could not be seeded")


def upload_preflight_archive() -> bytes:
    """Keep the original photo/context but reserve a distinct message for user 1."""
    output = io.BytesIO()
    with (
        zipfile.ZipFile(ROOT / "k6/fixtures/telegram-small.zip") as original,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as prepared,
    ):
        for item in original.infolist():
            content = original.read(item)
            if Path(item.filename).name == "result.json":
                export = json.loads(content)
                for index, message in enumerate(export["messages"]):
                    message["id"] = 900001 + index
                content = json.dumps(export).encode()
            prepared.writestr(item, content)
    return output.getvalue()


async def upload_preflight(directory: Path) -> None:
    info = state(directory)
    tokens = json.loads((directory / "tokens.json").read_text(encoding="utf-8"))
    trust = ssl.create_default_context(cafile=str(directory / "tls.crt"))
    async with httpx.AsyncClient(
        base_url="https://127.0.0.1:18000",
        headers={"Authorization": f"Bearer {tokens['jwt'][0]}"},
        verify=trust,
        timeout=20,
    ) as client:
        accepted = await client.post(
            "/api/v1/upload",
            files={
                "file": ("telegram-preflight.zip", upload_preflight_archive(), "application/zip")
            },
        )
        if accepted.status_code != 202:
            raise RuntimeError(f"Upload preflight acceptance HTTP {accepted.status_code}")
        status_url = accepted.json()["status_url"]
        for _ in range(90):
            response = await client.get(status_url)
            if response.status_code != 200:
                raise RuntimeError(f"Upload preflight status HTTP {response.status_code}")
            outcome = response.json()
            if outcome["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(1)
        if (
            outcome["status"] != "completed"
            or outcome["bets_processed"] != 1
            or outcome["bets_failed"] != 0
            or outcome["cost_usd"] != 0
        ):
            # Do not expose task inputs, identities, or raw error text in diagnostics.
            raise RuntimeError(
                "Upload preflight did not complete one cached bet without failure/cost"
            )
    conn = await connect(15432, info["admin_password"])
    try:
        persisted = await conn.fetchval(
            "SELECT count(*) FROM apostas WHERE usuario_id=1 AND origem='telegram'"
        )
        if persisted != 1:
            raise RuntimeError("Upload preflight did not persist exactly one tenant-owned bet")
    finally:
        await conn.close()
    info["upload_preflight_bets"] = 1
    write_private(directory / "state.json", info)
    evidence = {
        "sha": os.environ["TESTED_HEAD_SHA"],
        "checkout_sha": command(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "uploads": 1,
        "telegram_bets": persisted,
        "bets_failed": 0,
        "ai_cost_usd": 0,
        "transport": "verified HTTPS API and asynchronous worker callback",
    }
    (ROOT / "k6-upload-preflight-evidence.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )


def spawn(directory: Path, args: list[str], name: str, env: dict) -> None:
    info = state(directory)
    with (directory / f"{name}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env={**os.environ, **env},
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    info["processes"].append(process.pid)
    write_private(directory / "state.json", info)


async def regressions(directory: Path) -> None:
    info = state(directory)
    env = json.loads((directory / "env.json").read_text(encoding="utf-8"))
    conn = await connect(15432, info["admin_password"])
    try:
        await conn.execute("CREATE DATABASE k6_router_regression")
    finally:
        await conn.close()
    primary = f"postgresql+asyncpg://k6_admin:{info['admin_password']}@127.0.0.1:15432/k6_router_regression"
    replica = primary.replace(":15432/", ":15433/")
    mask(primary)
    mask(replica)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_router_db.py",
            "-n",
            "0",
            "-q",
            "--junitxml=k6-router-test-results.xml",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            **env,
            "TEST_DATABASE_URL": primary,
            "TEST_REPLICA_DATABASE_URL": replica,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    (directory / "router-tests.log").write_text(result.stdout, encoding="utf-8")
    report = ROOT / "k6-router-test-results.xml"
    if report.exists():
        sanitized = report.read_text(encoding="utf-8")
        for value in info.values():
            if isinstance(value, str) and len(value) > 16:
                sanitized = sanitized.replace(value, "[REDACTED]")
        report.write_text(sanitized, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Real primary/standby regression tests failed")
    root = ElementTree.parse(report).getroot()
    if list(root.iter("skipped")) or list(root.iter("failure")) or list(root.iter("error")):
        raise RuntimeError("Primary/standby regression must have no failures or skips")


async def start(directory: Path) -> None:
    env = json.loads((directory / "env.json").read_text(encoding="utf-8"))
    sys.stdout.write(
        json.dumps({"runner_cpus": os.cpu_count(), "http_workers": min(4, os.cpu_count() or 1)})
        + "\n"
    )
    spawn(
        directory,
        [
            sys.executable,
            "-m",
            "http.server",
            "18765",
            "--bind",
            "127.0.0.1",
            "--directory",
            str(directory / "issuer"),
        ],
        "issuer",
        env,
    )
    # API, primary, standby and k6 share this runner. Serialize materialization so
    # database writers do not compete with every HTTP process during a 100-user burst.
    for queue, concurrency in WORKER_CONCURRENCY.items():
        spawn(
            directory,
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "bancaemdia.workers.celery_app",
                "worker",
                "--queues",
                queue,
                "--concurrency",
                str(concurrency),
                "--hostname",
                f"{queue}@%h",
                "--loglevel",
                "WARNING",
            ],
            queue,
            env,
        )
    spawn(
        directory,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "k6_profiled_api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "18000",
            "--ssl-keyfile",
            str(directory / "tls.key"),
            "--ssl-certfile",
            str(directory / "tls.crt"),
            "--workers",
            str(min(4, os.cpu_count() or 1)),
            "--timeout-keep-alive",
            "30",
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        "api",
        env,
    )
    spawn(directory, [sys.executable, str(Path(__file__).resolve()), "refresh"], "refresh", env)
    tokens = json.loads((directory / "tokens.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(
        timeout=5, verify=ssl.create_default_context(cafile=env["SSL_CERT_FILE"])
    ) as client:
        for _ in range(90):
            try:
                ready = await client.get("https://127.0.0.1:18000/ready")
                panel = await client.get(
                    "https://127.0.0.1:18000/api/v1/painel",
                    headers={"Authorization": f"Bearer {tokens['jwt'][0]}"},
                )
                if ready.status_code == panel.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Real API, workers and authenticated dashboard are not ready")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as handle:
        handle.write(f"BASE_URL=https://127.0.0.1:18000\nSSL_CERT_FILE={env['SSL_CERT_FILE']}\n")
        # Stay below both GitHub's secret limit and Linux's per-environment-string limit.
        for index in range(5):
            handle.write(
                f"JWT_TOKENS_JSON_{index + 1}={json.dumps(tokens['jwt'][index * 50 : (index + 1) * 50])}\n"
            )
        handle.write(f"COLETA_TOKENS_JSON={json.dumps(tokens['coleta'])}\n")
    info = state(directory)
    info["cpu_baseline"] = process_cpu(info)
    statistics = await connect(15432, info["admin_password"])
    try:
        await statistics.execute("SELECT pg_stat_statements_reset()")
    finally:
        await statistics.close()
    write_private(directory / "state.json", info)


async def refresh(directory: Path) -> None:
    os.environ.update(json.loads((directory / "env.json").read_text(encoding="utf-8")))
    info = state(directory)
    from sqlalchemy.ext.asyncio import create_async_engine

    from bancaemdia.cli.refresh_painel import refresh_painel

    engine = create_async_engine(
        f"postgresql+asyncpg://k6_admin:{info['admin_password']}@127.0.0.1:15432/k6"
    )
    try:
        while True:
            await refresh_painel(engine)
            # Transactions provide a real WAL heartbeat, even in the read-only load profile.
            await asyncio.sleep(10)
    finally:
        await engine.dispose()


async def verify(directory: Path) -> None:
    info = state(directory)
    profile = os.environ.get("LOAD_PROFILE", "all")
    scenario_uploads = {"all": 250, "steady": 50, "spike": 200}.get(profile, 0)
    preflight_bets = info.get("upload_preflight_bets", 0)
    expected_uploads = scenario_uploads + preflight_bets
    conn = await connect(15432, info["admin_password"])
    try:
        for _ in range(60):
            pending = await conn.fetchval(
                "SELECT (SELECT count(*) FROM uploads WHERE status <> 'completed') + "
                "(SELECT count(*) FROM coletas_casa WHERE processado_em IS NULL)"
            )
            if pending == 0:
                break
            await asyncio.sleep(1)
        counts = dict(
            await conn.fetchrow(
                "SELECT (SELECT count(*) FROM usuarios) AS users,"
                "(SELECT count(*) FROM uploads WHERE status='completed') AS uploads,"
                "(SELECT count(*) FROM apostas WHERE origem='telegram') AS telegram_bets,"
                "(SELECT count(*) FROM apostas WHERE origem='casa') AS collection_bets,"
                "(SELECT count(*) FROM coletas_casa) AS raw_collections,"
                "(SELECT count(*) FROM coletas_casa WHERE processado_em IS NULL) AS pending_collections,"
                "(SELECT coalesce(sum(cost_usd),0) FROM uploads)::float8 AS ai_cost_usd,"
                "(SELECT count(*) FROM uploads WHERE status<>'completed' OR bets_failed<>0) AS failed_uploads"
            )
        )
        if counts["uploads"] != expected_uploads or counts["telegram_bets"] != expected_uploads:
            raise RuntimeError(f"Upload pipeline did not persist all expected bets: {counts}")
        if counts["failed_uploads"] or (
            profile in {"all", "coleta"} and counts["collection_bets"] < 100
        ):
            raise RuntimeError(f"Asynchronous materialization did not complete: {counts}")
        if counts["pending_collections"] or counts["collection_bets"] != counts["raw_collections"]:
            raise RuntimeError(f"Some accepted collections were not materialized: {counts}")
        if counts["ai_cost_usd"] != 0:
            raise RuntimeError("Unexpected AI cost in warmed-cache staging")
        role = await conn.fetchrow(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='k6_app'"
        )
        if role["rolsuper"] or role["rolbypassrls"]:
            raise RuntimeError("API role bypasses RLS")
        streaming = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_replication WHERE state='streaming'"
        )
        if streaming != 1:
            raise RuntimeError("Real streaming replica is unavailable")
        audit_size = dict(
            await conn.fetchrow(
                "SELECT count(*) AS rows, "
                "pg_total_relation_size('audit_log') AS total_bytes FROM audit_log"
            )
        )
        audit_resources = {
            row["resource_type"]: row["rows"]
            for row in await conn.fetch(
                "SELECT resource_type,count(*) AS rows FROM audit_log GROUP BY resource_type"
            )
        }
    finally:
        await conn.close()
    app = await connect(port=15432, user="k6_app", password=info["app_password"])
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.current_user_id','1',true)")
            leaked = await app.fetchval("SELECT count(*) FROM apostas WHERE usuario_id<>1")
            if leaked:
                raise RuntimeError("Cross-tenant rows visible to the API role")
    finally:
        await app.close()
    summary = json.loads((ROOT / "load-test-summary.json").read_text(encoding="utf-8"))
    for metric in summary["metrics"].values():
        if any(not threshold["ok"] for threshold in metric.get("thresholds", {}).values()):
            raise RuntimeError("k6 thresholds failed")
    evidence = {
        "sha": os.environ["TESTED_HEAD_SHA"],
        "checkout_sha": command(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "profile": profile,
        "environment": "ephemeral-github-runner-phase-0",
        "audit_storage": {**audit_size, "resource_counts": audit_resources},
        "live_process_cpu_seconds_since_ready": cpu_since_ready(info),
        "runner_cpus": os.cpu_count(),
        "http_workers": min(4, os.cpu_count() or 1),
        "celery_concurrency": WORKER_CONCURRENCY,
        "api_pool_per_engine": {
            "retained": 30,
            "overflow": 0,
            "prewarm": True,
            "recycle_seconds": 7200,
        },
        "counts": counts,
        "upload_expectation": {"scenario": scenario_uploads, "preflight": preflight_bets},
        "streaming_replicas": streaming,
        "rls_cross_tenant_visible": leaked,
        "ai": "synthetic cached extraction; no paid provider; not an AI benchmark",
        "deployment_acceptance": "No production hosting, TLS or launch capacity claim",
    }
    (ROOT / "k6-staging-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def cleanup(directory: Path) -> None:
    if not (directory / "state.json").exists():
        return
    info = state(directory)
    for pid in info["processes"]:
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes()
            marker = f"K6_STAGING_DIR={directory}".encode()
            if marker in environment.split(b"\0") and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError):
            pass
    containers = command([
        "docker",
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label={LABEL}={info['name']}",
    ]).split()
    if containers:
        command(["docker", "rm", "--force", "--volumes", *containers])
    networks = command([
        "docker",
        "network",
        "ls",
        "--quiet",
        "--filter",
        f"label={LABEL}={info['name']}",
    ]).split()
    for network in networks:
        command(["docker", "network", "rm", network])


async def diagnose(directory: Path) -> None:
    """Write sanitized tails to the job log, never upload private runtime files."""
    if not (directory / "state.json").exists():
        return
    sys.stdout.write(
        json.dumps({"live_process_cpu_seconds_since_ready": cpu_since_ready(state(directory))})
        + "\n"
    )
    info = state(directory)
    statistics = await connect(15432, info["admin_password"])
    try:
        # Never print query text: only numeric measurements and known schema names.
        from bancaemdia import models

        known = set(models.Aposta.metadata.tables) | {
            "billing_require_write",
            "require_active_tenant",
            "audit_tenant_write",
            "set_config",
            "pg_advisory_xact_lock",
            "clock_timestamp",
        }
        measurements = []
        for row in await statistics.fetch(
            "SELECT query,calls,total_exec_time,mean_exec_time,max_exec_time,rows,"
            "shared_blks_hit,shared_blks_read,temp_blks_written FROM pg_stat_statements "
            "ORDER BY total_exec_time DESC LIMIT 25"
        ):
            values = dict(row)
            query = values.pop("query")
            values["query_sha256"] = hashlib.sha256(query.encode()).hexdigest()
            values["schema_names"] = sorted(
                name for name in known if re.search(rf"\b{re.escape(name)}\b", query)
            )
            measurements.append(values)
        sys.stdout.write(json.dumps({"postgres_query_measurements": measurements}) + "\n")
    finally:
        await statistics.close()
    for path in directory.glob("api-*.prof"):
        profile = pstats.Stats(str(path))
        hottest = sorted(profile.stats.items(), key=lambda entry: entry[1][2], reverse=True)[:20]
        sys.stdout.write(
            json.dumps({
                "api_cpu_profile": [
                    {
                        "module": Path(key[0]).name,
                        "line": key[1],
                        "function": key[2],
                        "calls": value[1],
                        "cpu_self_seconds": value[2],
                        "cpu_total_seconds": value[3],
                    }
                    for key, value in hottest
                ]
            })
            + "\n"
        )
    # Only exception classes and known routine names, never task arguments,
    # exception reprs, SQL parameters or identifiers, enter public diagnostics.
    import redis

    env = json.loads((directory / "env.json").read_text(encoding="utf-8"))
    failures: Counter[str] = Counter()
    try:
        with redis.Redis.from_url(env["CELERY_BROKER_URL"], socket_timeout=2) as broker:
            for encoded in broker.lrange("dead_letter", 0, 99):
                exception = str(json.loads(encoded).get("headers", {}).get("exception", ""))
                kind = exception.split("(", 1)[0]
                if kind.isidentifier():
                    failures[kind] += 1
                for routine in ("telegram_legacy_media_visible", "telegram_chat_linked"):
                    if re.search(rf"permission denied for function {routine}\b", exception):
                        failures[f"permission_denied:{routine}"] += 1
    except (redis.RedisError, ValueError):
        sys.stdout.write("Dead-letter diagnostics unavailable\n")
    sys.stdout.write(json.dumps({"dead_letter_exception_counts": dict(failures)}) + "\n")
    hidden = [value for value in state(directory).values() if isinstance(value, str)]
    for name in ("tokens.json", "env.json"):
        path = directory / name
        if path.exists():
            for value in json.loads(path.read_text(encoding="utf-8")).values():
                hidden.extend(value if isinstance(value, list) else [value])
    for path in directory.glob("*.log"):
        tail = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
        for secret in sorted(
            (x for x in hidden if isinstance(x, str) and len(x) > 12), key=len, reverse=True
        ):
            tail = tail.replace(secret, "[REDACTED]")
        sys.stdout.write(f"{path.name}\n{tail}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=[
            "prepare",
            "regressions",
            "start",
            "refresh",
            "upload-preflight",
            "verify",
            "cleanup",
            "diagnose",
        ],
    )
    operation = parser.parse_args().operation
    directory = runtime_dir()
    if operation == "cleanup":
        cleanup(directory)
    elif operation == "diagnose":
        await diagnose(directory)
    else:
        await {
            "prepare": prepare,
            "regressions": regressions,
            "start": start,
            "refresh": refresh,
            "upload-preflight": upload_preflight,
            "verify": verify,
        }[operation](directory)


if __name__ == "__main__":
    asyncio.run(main())
