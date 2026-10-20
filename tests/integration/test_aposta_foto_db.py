"""Mandatory real API/PostgreSQL/RLS and local HTTP S3 evidence acceptance."""

import asyncio
import hashlib
import io
import json
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import boto3
import httpx
import jwt
import pytest
from botocore.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import delete, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia import models
from bancaemdia.auth import middleware as authentication
from bancaemdia.auth.identity_crypto import digest
from bancaemdia.auth.identity_service import IdentityService
from bancaemdia.auth.jwt import JWKSCache
from bancaemdia.config import get_settings
from bancaemdia.core.context import current_user_id
from bancaemdia.db.session import get_db, get_db_primary_snapshot, get_db_snapshot
from bancaemdia.integrations.telegram.codec import _fernet
from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.mensagem_repo import MidiaArquivoRepo, MidiaRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.services.telegram_confirmation import confirm_draft
from bancaemdia.services.telegram_conversation import open_draft
from bancaemdia.storage import midia_s3
from bancaemdia.workers import materialization, upload
from tests.identity.support import key_settings
from tests.integration.test_telegram_confirmation import _account

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("postgres")]


def image_bytes(format_: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 5), tuple(uuid4().bytes[:3])).save(buffer, format=format_)
    return buffer.getvalue()


def url(key: str) -> str:
    return "/api/v1/apostas/" + quote(key, safe="") + "/foto"


@dataclass
class Api:
    client: httpx.AsyncClient
    key: Any

    def headers(self, user: int, *, seconds: int = 600) -> dict[str, str]:
        settings = get_settings()
        token = jwt.encode(
            {
                "sub": str(user),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "exp": int(time.time()) + seconds,
            },
            self.key,
            algorithm="RS256",
            headers={"kid": "photo-test"},
        )
        return {"Authorization": "Bearer " + token}


@pytest.fixture
async def api(engine_app: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    from bancaemdia.auth.identity_config import identity_settings
    from bancaemdia.main import app

    monkeypatch.setenv("AUTH_ENABLED", "false")
    identity_settings.cache_clear()
    get_settings.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="photo-test", alg="RS256", use="sig")
    cache = JWKSCache(None, "RS256")
    cache.keys = {"photo-test": public}
    cache.fetched_at = time.monotonic()
    monkeypatch.setattr(authentication, "get_jwks_cache", lambda: cache)

    async def database() -> AsyncIterator[AsyncSession]:
        async with engine_app.connect() as connection:
            await connection.execution_options(isolation_level="REPEATABLE READ")
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": current_user_id.get() or ""},
                )
                yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update({
        get_db_primary_snapshot: database,
        get_db: database,
        get_db_snapshot: database,
    })
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://api.test",
        ) as client:
            yield Api(client, key)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        identity_settings.cache_clear()
        get_settings.cache_clear()


async def save_photo(
    engine: AsyncEngine, como: Any, user: int, content: bytes, mime: str = "image/png"
) -> str:
    hash_ = hashlib.sha256(content).hexdigest()
    async with como(engine, user) as session:
        await MidiaRepo().upsert_idempotent(session, hash_, mime, len(content))
        await MidiaArquivoRepo().upsert_idempotent(session, hash_, content)
        await session.commit()
    return hash_


async def bet(
    engine: AsyncEngine, como: Any, user: int, hash_: str | None, *, review: bool = False
) -> str:
    key = "m:" + uuid4().hex
    async with como(engine, user) as session:
        payload = {
            "origem": "manual",
            "casa": "Betano",
            "odd": 2.0,
            "stake_unidades": 1.0,
            "valor_unidade_centavos": 10000,
            "selecionada": True,
            "revisao_grave": review,
            "estado": "PENDENTE",
        }
        await EventoRepo().append(
            session,
            {
                "usuario_id": user,
                "tipo": "APOSTA_CRIADA",
                "fonte": "manual",
                "payload_json": payload,
                "aposta_chave": key,
            },
        )
        saved = await ApostaRepo().upsert_materializada(
            session,
            {
                "usuario_id": user,
                "chave": key,
                "origem": "manual",
                "stake_unidades": 1.0,
                "stake_centavos": 10000,
                "valor_aposta_centavos": 10000,
                "odd": 2.0,
                "revisao_grave": review,
                "midia_hash": hash_,
            },
        )
        assert saved is not None
        await session.commit()
    return key


async def review(
    engine: AsyncEngine, como: Any, user: int, key: str, hash_: str, *, resolved: bool = False
) -> int:
    async with como(engine, user) as session:
        created = await RevisaoPendenteRepo().create(
            session,
            {
                "usuario_id": user,
                "midia_hash": hash_,
                "motivo": "ilegivel",
                "extracao_bruta": {"aposta_chave": key},
            },
        )
        if resolved:
            await RevisaoPendenteRepo().resolve(session, user, created.id)
        await session.commit()
    return created.id


def private(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert {"authorization", "cookie"} <= {
        value.strip().lower() for value in response.headers["vary"].split(",")
    }
    assert "location" not in response.headers and "etag" not in response.headers


async def snapshot(engine: AsyncEngine, como: Any, user: int) -> dict[str, Any]:
    result = {}
    async with como(engine, user) as session:
        for table in ("apostas", "eventos", "aposta_consolidacoes", "movimentos"):
            result[table] = await session.scalar(
                text(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id), '[]'::jsonb) FROM {table} t WHERE usuario_id=:uid"
                ),
                {"uid": user},
            )
    return result


async def test_ownership_deduplication_known_hash_and_complete_read_invariance(
    api: Api, engine_app: AsyncEngine, engine_admin: AsyncEngine, como: Any, novo_usuario: Any
) -> None:
    owner, other = await novo_usuario(), await novo_usuario()
    content = image_bytes()
    hash_ = await save_photo(engine_app, como, owner, content)
    own_key = await bet(engine_app, como, owner, hash_)
    other_key = await bet(engine_app, como, other, hash_)
    no_photo = await bet(engine_app, como, other, None)
    before = await snapshot(engine_app, como, owner)
    async with engine_admin.connect() as conn:
        role = (
            await conn.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='bancaemdia_app'")
            )
        ).one()
        assert role == (False, False)
        assert await conn.scalar(
            text("SELECT relforcerowsecurity FROM pg_class WHERE relname='apostas'")
        )
    for user, key in ((owner, own_key), (other, other_key)):
        response = await api.client.get(url(key), headers=api.headers(user))
        assert response.status_code == 200 and response.content == content
        private(response)
        assert response.headers["content-disposition"] == 'inline; filename="foto-aposta.png"'
    forbidden = await api.client.get(url(own_key), headers=api.headers(other))
    missing = await api.client.get(url("inexistente"), headers=api.headers(other))
    assert (
        forbidden.status_code == missing.status_code == 404 and forbidden.json() == missing.json()
    )
    for key in (no_photo, hash_):
        response = await api.client.get(url(key), headers=api.headers(other))
        assert response.status_code == 404
        private(response)
    assert await snapshot(engine_app, como, owner) == before
    detail = await api.client.get("/api/v1/apostas/" + own_key, headers=api.headers(owner))
    assert detail.status_code == 200
    assert "base64" not in detail.text and content.hex() not in detail.text


async def test_review_resolution_and_deleted_restored_bet_keep_original(
    api: Api, engine_app: AsyncEngine, como: Any, novo_usuario: Any
) -> None:
    owner = await novo_usuario()
    content = image_bytes()
    hash_ = await save_photo(engine_app, como, owner, content)
    key = await bet(engine_app, como, owner, hash_, review=True)
    rid = await review(engine_app, como, owner, key, hash_)
    headers = api.headers(owner)
    assert (await api.client.get(url(key), headers=headers)).content == content
    resolved = await api.client.post(
        f"/api/v1/revisao/{rid}/resolver",
        headers=headers,
        json={"acao": "CORRIGIR", "aposta_corrigida": {"odd": 2.0, "stake_unidades": 1.0}},
    )
    assert resolved.status_code == 200, resolved.text
    assert (await api.client.get(f"/api/v1/revisao/{rid}/foto", headers=headers)).status_code == 404
    after_resolution = await snapshot(engine_app, como, owner)
    assert (await api.client.get(url(key), headers=headers)).content == content
    assert await snapshot(engine_app, como, owner) == after_resolution
    deleted = await api.client.delete("/api/v1/apostas/" + key, headers=headers)
    assert deleted.status_code == 200
    assert (await api.client.get(url(key), headers=headers)).content == content
    restored = await api.client.post("/api/v1/apostas/" + key + "/restaurar", headers=headers)
    assert restored.status_code == 200
    assert (await api.client.get(url(key), headers=headers)).content == content


async def test_exact_resolved_review_provenance_and_ambiguity_fail_closed(
    api: Api, engine_app: AsyncEngine, como: Any, novo_usuario: Any
) -> None:
    owner, other = await novo_usuario(), await novo_usuario()
    content = image_bytes()
    hash_ = await save_photo(engine_app, como, owner, content)
    key = await bet(engine_app, como, owner, None)
    await review(engine_app, como, other, key, hash_, resolved=True)
    assert (await api.client.get(url(key), headers=api.headers(owner))).status_code == 404
    await review(engine_app, como, owner, key, hash_, resolved=True)
    await review(engine_app, como, owner, key, hash_)
    assert (await api.client.get(url(key), headers=api.headers(owner))).content == content
    second_hash = await save_photo(engine_app, como, owner, image_bytes())
    await review(engine_app, como, owner, key, second_hash, resolved=True)
    assert (await api.client.get(url(key), headers=api.headers(owner))).status_code == 404


@pytest.mark.parametrize(
    "format_,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
async def test_real_validated_formats_are_delivered_without_modification(
    api: Api, engine_app: AsyncEngine, como: Any, novo_usuario: Any, format_: str, mime: str
) -> None:
    owner = await novo_usuario()
    content = image_bytes(format_)
    hash_ = await save_photo(engine_app, como, owner, content, mime)
    key = await bet(engine_app, como, owner, hash_)
    response = await api.client.get(url(key), headers=api.headers(owner))
    assert (
        response.status_code == 200
        and response.content == content
        and response.headers["content-type"] == mime
    )
    private(response)


@pytest.mark.parametrize(
    "invalid",
    [
        "active",
        "mismatch",
        "corrupt",
        "png_checksum",
        "truncated_jpeg",
        "animation_limit",
        "oversized",
        "hash_mismatch",
        "missing_blob",
    ],
)
async def test_invalid_missing_and_oversized_database_images(
    api: Api, engine_app: AsyncEngine, como: Any, novo_usuario: Any, invalid: str
) -> None:
    owner = await novo_usuario()
    content = image_bytes()
    mime = "image/png"
    if invalid == "active":
        content, mime = (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "image/svg+xml",
        )
    elif invalid == "mismatch":
        mime = "image/jpeg"
    elif invalid == "corrupt":
        content = b"\x89PNG\r\n\x1a\ninvalid"
    elif invalid == "png_checksum":
        malformed = bytearray(content)
        malformed[29] ^= 1
        content = bytes(malformed)
    elif invalid == "truncated_jpeg":
        content, mime = image_bytes("JPEG")[:-2], "image/jpeg"
    elif invalid == "animation_limit":
        frames = [Image.new("RGB", (8, 5), (index % 256, index // 256, 0)) for index in range(257)]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], disposal=2)
        content, mime = buffer.getvalue(), "image/gif"
    elif invalid == "oversized":
        content = b"x" * (MAX_PHOTO_BYTES + 1)
    hash_ = await save_photo(engine_app, como, owner, content, mime)
    key = await bet(engine_app, como, owner, hash_)
    if invalid in {"hash_mismatch", "missing_blob"}:
        async with como(engine_app, owner) as session:
            if invalid == "missing_blob":
                await session.execute(
                    delete(models.MidiaArquivo).where(models.MidiaArquivo.hash == hash_)
                )
            else:
                await session.execute(
                    update(models.MidiaArquivo)
                    .where(models.MidiaArquivo.hash == hash_)
                    .values(conteudo=image_bytes())
                )
            await session.commit()
    response = await api.client.get(url(key), headers=api.headers(owner))
    assert response.status_code == (404 if invalid == "missing_blob" else 422), response.text
    private(response)
    assert (
        await api.client.get("/api/v1/apostas/" + key, headers=api.headers(owner))
    ).status_code == 200


@dataclass
class ObjectStore:
    objects: dict[str, bytes]
    mode: str = "ok"
    reads: int = 0


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[ObjectStore]:
    store = ObjectStore({})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_PUT(self) -> None:
            store.objects[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            store.reads += 1
            assert self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
            if store.mode in {"missing", "unavailable"} or self.path not in store.objects:
                code = "ServiceUnavailable" if store.mode == "unavailable" else "NoSuchKey"
                body = f"<Error><Code>{code}</Code></Error>".encode()
                self.send_response(503 if store.mode == "unavailable" else 404)
            else:
                body = store.objects[self.path] if store.mode != "corrupt" else b"corrupted-object"
                self.send_response(200)
            self.send_header(
                "Content-Length",
                str(MAX_PHOTO_BYTES + 1 if store.mode == "oversized" else len(body)),
            )
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{server.server_port}",
        aws_access_key_id="disposable",
        aws_secret_access_key="synthetic-only",
        region_name="us-east-1",
        config=Config(
            retries={"max_attempts": 0},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    monkeypatch.setattr(midia_s3, "_client", lambda: client)
    monkeypatch.setenv("S3_UPLOAD_BUCKET", "private-test")
    get_settings.cache_clear()
    try:
        yield store
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "mode,expected",
    [("ok", 200), ("missing", 404), ("unavailable", 503), ("corrupt", 422), ("oversized", 422)],
)
async def test_s3_authenticated_api_transport_and_storage_errors(
    api: Api,
    storage: ObjectStore,
    engine_app: AsyncEngine,
    como: Any,
    novo_usuario: Any,
    mode: str,
    expected: int,
) -> None:
    owner, other = await novo_usuario(), await novo_usuario()
    content = image_bytes()
    hash_ = await save_photo(engine_app, como, owner, content)
    key = await bet(engine_app, como, owner, hash_)
    storage.mode = mode
    forbidden = await api.client.get(url(key), headers=api.headers(other))
    assert forbidden.status_code == 404 and storage.reads == 0
    response = await api.client.get(url(key), headers=api.headers(owner))
    assert response.status_code == expected, response.text
    assert storage.reads == 1
    private(response)
    assert "media/" not in str(response.headers) and "private-test" not in response.text
    if expected == 200:
        assert response.content == content
    if expected == 503:
        assert response.headers["retry-after"] == "5"
        storage.mode = "ok"
        assert (await api.client.get(url(key), headers=api.headers(owner))).content == content


async def test_read_only_is_explicitly_allowed_and_photo_has_no_financial_writes(
    api: Api, engine_app: AsyncEngine, engine_admin: AsyncEngine, como: Any, novo_usuario: Any
) -> None:
    from bancaemdia.domain.access import READ_OPERATION_MODES
    from bancaemdia.domain.billing import AccessMode
    from bancaemdia.main import app

    owner = await novo_usuario()
    content = image_bytes()
    hash_ = await save_photo(engine_app, como, owner, content)
    key = await bet(engine_app, como, owner, hash_)
    async with engine_admin.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("SELECT billing_activate_rollout()"))
            await connection.execute(text("SET LOCAL ROLE bancaemdia_app"))
            before = await snapshot(engine_app, como, owner)
            # Unconfirmed/no subscription is READ_ONLY; this is not granted by the HTTP verb.
            from bancaemdia.repositories.assinatura_repo import AssinaturaRepo

            async def read_only_database() -> AsyncIterator[AsyncSession]:
                async with AsyncSession(
                    bind=connection, join_transaction_mode="create_savepoint"
                ) as session:
                    await session.execute(
                        text("SELECT set_config('app.current_user_id', :uid, true)"),
                        {"uid": current_user_id.get() or str(owner)},
                    )
                    assert await session.scalar(text("SELECT current_user")) == "bancaemdia_app"
                    yield session

            previous = app.dependency_overrides[get_db_primary_snapshot]
            app.dependency_overrides[get_db_primary_snapshot] = read_only_database
            async with AsyncSession(
                bind=connection, join_transaction_mode="create_savepoint"
            ) as session:
                await session.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(owner)},
                )
                assert (
                    await AssinaturaRepo().read_status(session, owner)
                ).access == AccessMode.READ_ONLY
            assert AccessMode.READ_ONLY in READ_OPERATION_MODES["aposta_foto"]
            assert (await api.client.get(url(key), headers=api.headers(owner))).content == content
            assert await snapshot(engine_app, como, owner) == before
        finally:
            if "previous" in locals():
                app.dependency_overrides[get_db_primary_snapshot] = previous
            await transaction.rollback()


@pytest.mark.parametrize("credential", ["missing", "invalid", "expired", "inactive"])
async def test_401_and_private_headers_for_session_failure(
    api: Api, engine_app: AsyncEngine, como: Any, novo_usuario: Any, credential: str
) -> None:
    owner = await novo_usuario()
    headers = (
        {}
        if credential == "missing"
        else api.headers(owner, seconds=-1 if credential == "expired" else 600)
    )
    if credential == "invalid":
        headers = {"Authorization": "Bearer invalid"}
    if credential == "inactive":
        async with como(engine_app, owner) as session:
            await session.execute(
                update(models.Usuario).where(models.Usuario.id == owner).values(ativo=False)
            )
            await session.commit()
    response = await api.client.get(url("inexistente"), headers=headers)
    assert response.status_code == 401 and "sessão" in response.json()["detail"]
    private(response)


async def test_photo_quota_uses_existing_429_retry_after(
    api: Api, monkeypatch: pytest.MonkeyPatch, novo_usuario: Any
) -> None:
    monkeypatch.setenv("API_RATE_LIMIT", "1/minute")
    get_settings.cache_clear()
    owner = await novo_usuario()
    assert (await api.client.get(url("missing"), headers=api.headers(owner))).status_code == 404
    response = await api.client.get(url("missing"), headers=api.headers(owner))
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    private(response)


async def test_original_export_ingestion_and_replay_preserve_provenance(
    api: Api, engine_app: AsyncEngine, como: Any, novo_usuario: Any
) -> None:
    owner = await novo_usuario()
    content = image_bytes()
    chat, message = uuid4().int % 10**9, uuid4().int % 10**9
    buffer = io.BytesIO()
    folder = "ChatExport/"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            folder + "result.json",
            json.dumps({
                "name": "Sintético",
                "type": "channel",
                "id": chat,
                "messages": [
                    {
                        "id": message,
                        "type": "message",
                        "date": "2026-10-01T12:00:00",
                        "text": "1 unidade",
                        "photo": "photos/fixture.png",
                    }
                ],
            }),
        )
        archive.writestr(folder + "photos/fixture.png", content)
    async with como(engine_app, owner) as session:
        stored = models.Upload(usuario_id=owner, filename="ChatExport.zip")
        session.add(stored)
        await session.flush()
        session.add(
            models.UploadArquivo(upload_id=stored.id, usuario_id=owner, conteudo=buffer.getvalue())
        )
        upload_id = stored.id
        await session.commit()
    result = await upload._guardar_export(engine_app, owner, upload_id)
    assert result == (1, 1)
    hash_ = hashlib.sha256(content).hexdigest()
    reading = {
        "chat_id": chat,
        "message_id": message,
        "postada_em": "2026-10-01T12:00:00",
        "bilhete": {
            "casa": "Betano",
            "odd_total": 2.0,
            "stake_unidades": 1.0,
            "evento": "Jogo sintético",
            "confianca": 0.95,
        },
        "grave": False,
        "cupons": [],
    }
    extracted = materialization.ExtracaoDoJson.model_validate(reading)
    saved = await materialization.gravar_leitura(engine_app, owner, extracted, reading, hash_)
    key = saved[0].chave
    response = await api.client.get(url(key), headers=api.headers(owner))
    assert response.status_code == 200 and response.content == content
    async with como(engine_app, owner) as session:
        events = await EventoRepo().list_by_aposta_chave(session, owner, key)
        assert events[0].payload_json["snapshot_replay"]["midia_hash"] == hash_


@pytest.mark.parametrize(
    "retention",
    [
        "live",
        "expired",
        "removed",
        "unlink",
        "bad_ciphertext",
        "wrong_reference",
        "legacy_without_bet_hash",
    ],
)
async def test_bot_confirmation_private_image_and_retention(
    api: Api,
    engine_app: AsyncEngine,
    engine_admin: AsyncEngine,
    como: Any,
    novo_usuario: Any,
    retention: str,
) -> None:
    owner, other = await novo_usuario(), await novo_usuario()
    account = await _account(engine_admin, engine_app, como, owner)
    chat = uuid4().int % 10**9
    content = image_bytes()
    hash_ = hashlib.sha256(content).hexdigest()
    # The same hash exists in another user's export. It cannot resurrect deleted bot media.
    await save_photo(engine_app, como, other, content)
    async with como(engine_app, owner) as session:
        session.add(
            models.TelegramLink(usuario_id=owner, telegram_user_id=chat, telegram_chat_id=chat)
        )
        draft, _ = await open_draft(
            session,
            user_id=owner,
            chat_id=chat,
            message_id=1,
            update_id=uuid4().int % 10**9,
            media_hash=hash_,
            extracted={
                "casa": "Betano",
                "conta_casa_id": account,
                "odd": 2.0,
                "stake_unidades": 1.0,
                "data_aposta": "2026-10-01T12:00:00-03:00",
                "data_jogo": "2026-10-02T12:00:00-03:00",
            },
        )
        session.add(
            models.TelegramMedia(
                usuario_id=owner,
                draft_id=draft.id,
                content_hash=hash_,
                mime="image/png",
                content_ciphertext=_fernet().encrypt(content),
            )
        )
        await session.flush()
        reply = await confirm_draft(
            session, user_id=owner, chat_id=chat, update_id=uuid4().int % 10**9
        )
        assert reply.queued
        key = "telegram_draft:" + str(draft.id)
        persisted = await ApostaRepo().get_by_chave(session, owner, key)
        assert persisted is not None and persisted.midia_hash == hash_
        if retention == "expired":
            draft.closed_at = datetime.now(UTC) - timedelta(
                days=get_settings().TELEGRAM_MEDIA_RETENTION_DAYS, seconds=1
            )
        elif retention == "removed":
            await session.execute(
                delete(models.TelegramMedia).where(models.TelegramMedia.draft_id == draft.id)
            )
        elif retention == "bad_ciphertext":
            await session.execute(
                update(models.TelegramMedia)
                .where(models.TelegramMedia.draft_id == draft.id)
                .values(content_ciphertext=b"invalid")
            )
        elif retention == "wrong_reference":
            await session.execute(
                update(models.RascunhoAposta)
                .where(models.RascunhoAposta.id == draft.id)
                .values(media_hash="f" * 64)
            )
        elif retention == "legacy_without_bet_hash":
            await session.execute(
                update(models.Aposta).where(models.Aposta.chave == key).values(midia_hash=None)
            )
        await session.commit()
    if retention == "unlink":
        unlinked = await api.client.delete("/api/v1/telegram/link", headers=api.headers(owner))
        assert unlinked.status_code == 200 and unlinked.json() == {"revoked": True}
    before = await snapshot(engine_app, como, owner)
    response = await api.client.get(url(key), headers=api.headers(owner))
    expected = (
        503
        if retention == "bad_ciphertext"
        else 200
        if retention in {"live", "legacy_without_bet_hash"}
        else 404
    )
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.content == content
    assert (await api.client.get(url(key), headers=api.headers(other))).status_code == 404
    assert (
        await api.client.get("/api/v1/apostas/" + key, headers=api.headers(owner))
    ).status_code == 200
    assert await snapshot(engine_app, como, owner) == before
    private(response)


async def test_real_cookie_session_revocation_blocks_previous_photo_access(
    api: Api,
    banco: Any,
    engine_admin: AsyncEngine,
    engine_app: AsyncEngine,
    como: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bancaemdia.auth import middleware, transport

    role = "photo_auth_" + uuid4().hex
    password = uuid4().hex
    async with engine_admin.begin() as connection:
        await connection.execute(
            text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOBYPASSRLS")
        )
        await connection.execute(text(f"GRANT bancaemdia_auth TO {role}"))
    connection_url = (
        make_url(banco.url_admin)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )
    auth_engine = create_async_engine(connection_url, poolclass=NullPool, hide_parameters=True)
    settings = key_settings(
        tmp_path,
        AUTH_ENABLED=True,
        AUTH_DATABASE_URL=SecretStr(connection_url),
        AUTH_PUBLIC_URL="https://api.test",
        AUTH_FRONTEND_ORIGIN="https://api.test",
        OIDC_ISSUER="https://issuer.test",
        OIDC_CLIENT_ID="fixture",
    )
    service = IdentityService(settings, auth_engine)
    monkeypatch.setattr(middleware, "identity_settings", lambda: settings, raising=False)
    # These functions are imported inside dispatch, so patch their source modules too.
    monkeypatch.setattr("bancaemdia.auth.identity_config.identity_settings", lambda: settings)
    monkeypatch.setattr("bancaemdia.auth.identity_service.identity_service", lambda: service)
    monkeypatch.setattr(transport, "identity_settings", lambda: settings)
    monkeypatch.setattr(transport, "identity_service", lambda: service)
    sid, cookie = str(uuid4()), uuid4().hex
    try:
        await service.verify_database_role()
        async with AsyncSession(auth_engine) as session, session.begin():
            owner, identity = await service.provision(
                session,
                {
                    "iss": settings.OIDC_ISSUER,
                    "sub": uuid4().hex,
                    "email_verified": True,
                    "email": uuid4().hex + "@test.invalid",
                    "name": "Photo fixture",
                },
            )
            sealed = service._credentials(
                sid, owner, 0, {"refresh_token": "synthetic", "id_token": "synthetic"}
            )
            await session.execute(
                text(
                    "INSERT INTO auth_private.sessions(id,identity_id,cookie_hash,encrypted,expires_at,idle_until,access_until) VALUES(:id,:identity,:cookie,:encrypted,now()+interval '1 hour',now()+interval '1 hour',now()+interval '5 minutes')"
                ),
                {"id": sid, "identity": identity, "cookie": digest(cookie), "encrypted": sealed},
            )
        content = image_bytes()
        hash_ = await save_photo(engine_app, como, owner, content)
        key = await bet(engine_app, como, owner, hash_)
        headers = {"Cookie": settings.session_cookie + "=" + cookie}
        assert (await api.client.get(url(key), headers=headers)).content == content
        async with auth_engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_private.sessions SET revoked_at=now() WHERE id=:id"), {"id": sid}
            )
        response = await api.client.get(url(key), headers=headers)
        assert response.status_code == 401
        private(response)
    finally:
        await auth_engine.dispose()
        async with engine_admin.begin() as connection:
            await connection.execute(text(f"DROP ROLE {role}"))


@pytest.mark.parametrize(
    "failure",
    ["database", "unexpected", "invalid_key", "empty_key", "storage_config", "storage_pointer"],
)
async def test_private_validation_and_dependency_failures(
    api: Api,
    engine_app: AsyncEngine,
    como: Any,
    novo_usuario: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    owner = await novo_usuario()
    content = image_bytes()
    hash_ = await save_photo(engine_app, como, owner, content)
    key = await bet(engine_app, como, owner, hash_)
    if failure in {"database", "unexpected"}:

        async def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(0)
            if failure == "unexpected":
                raise RuntimeError("private credentials and media information")
            raise DBAPIError(None, None, RuntimeError("private connection information"))

        monkeypatch.setattr(ApostaRepo, "get_by_chave", unavailable)
    elif failure == "invalid_key":
        key = "x" * 257
    elif failure == "empty_key":
        key = ""
    else:
        monkeypatch.setenv(
            "S3_UPLOAD_BUCKET", "private-test" if failure == "storage_pointer" else ""
        )
        get_settings.cache_clear()
        async with como(engine_app, owner) as session:
            await session.execute(
                update(models.MidiaArquivo)
                .where(models.MidiaArquivo.hash == hash_)
                .values(s3_key="private-invalid-pointer", conteudo=b"")
            )
            await session.commit()
    target = "/api/v1/apostas/foto" if failure == "empty_key" else url(key)
    response = await api.client.get(target, headers=api.headers(owner))
    assert response.status_code == (
        500
        if failure == "unexpected"
        else 503
        if failure in {"database", "storage_config"}
        else 404
        if failure == "empty_key"
        else 422
    ), response.text
    assert "private" not in response.text and "connection" not in response.text
    if response.status_code == 503:
        assert response.headers["retry-after"] == "5"
    private(response)


async def test_photo_reads_preserve_nonempty_consolidations_and_cash(
    api: Api,
    engine_app: AsyncEngine,
    engine_admin: AsyncEngine,
    novo_usuario: Any,
    como: Any,
) -> None:
    from tests.integration.cruzamento import test_consolidacao

    support: Any = test_consolidacao

    owner = await novo_usuario()
    house, ids = await support.accounts(engine_admin, engine_app, owner)
    ticket = "photo-" + uuid4().hex
    casa = await support.intake_house(engine_app, owner, house, support.house_payload(ticket))
    await support.intake_telegram(engine_app, owner, support.telegram_payload(ticket))
    content = image_bytes()
    hash_ = await save_photo(engine_app, como, owner, content)
    async with como(engine_app, owner) as session:
        record = await session.get(models.Aposta, casa)
        assert record is not None
        key = record.chave
        record.midia_hash = hash_
        session.add(
            models.Movimento(
                usuario_id=owner,
                conta_casa_id=ids[0],
                tipo="DEPOSITO",
                valor_centavos=50000,
                ocorrido_em=support.GAME,
            )
        )
        await session.commit()
    before = await snapshot(engine_app, como, owner)
    assert all(before[table] for table in before)
    for _ in range(2):
        response = await api.client.get(url(key), headers=api.headers(owner))
        assert response.status_code == 200 and response.content == content
    assert await snapshot(engine_app, como, owner) == before
