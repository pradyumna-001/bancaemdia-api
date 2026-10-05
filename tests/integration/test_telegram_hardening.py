"""HTTP → restricted PostgreSQL → real extraction/cache/limiter → fake Bot API.

External Telegram and AI are the only substitutes. No in-memory queue/session
survives between steps. Missing PostgreSQL/Redis is an error in this suite.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
import redis
from PIL import Image
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bancaemdia import models
from bancaemdia.cache.extracao_cache import ExtracaoCache
from bancaemdia.config import get_settings
from bancaemdia.extracao.cliente import Leitura
from bancaemdia.extracao.modelos import ExtracaoBilhete
from bancaemdia.integrations.telegram import webhook
from bancaemdia.integrations.telegram.client import TelegramClient
from bancaemdia.integrations.telegram.codec import decrypt_payload
from bancaemdia.main import app
from bancaemdia.rate_limit.anthropic_limiter import AnthropicLimiter
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo
from bancaemdia.services.telegram_abuse import admit
from bancaemdia.services.telegram_link import issue_code
from bancaemdia.workers import extraction, materialization, telegram, telegram_extraction
from bancaemdia.workers.telegram_privacy import purge_telegram

pytestmark = pytest.mark.xdist_group("postgres")


@pytest.fixture(scope="module", autouse=True)
def require_real_infrastructure():
    if not os.environ.get("TEST_DATABASE_URL"):
        from testcontainers.core.docker_client import DockerClient

        assert DockerClient().client.ping(), "Telegram hardening requires real PostgreSQL"


@pytest.fixture(scope="module")
def bot_redis():
    url = os.environ.get("TEST_REDIS_URL")
    if url:
        client = redis.Redis.from_url(url)
        assert client.ping()
        yield client
        client.close()
    else:
        from testcontainers.redis import RedisContainer

        with RedisContainer("redis:7-alpine") as container:
            client = container.get_client()
            assert client.ping()
            yield client
            client.close()


class FakeProviders:
    modelo = "claude-haiku-4-5"
    modelo_escalonamento = "claude-sonnet-5"

    def __init__(self):
        self.coupons = [
            ExtracaoBilhete(
                casa="Betano",
                odd_total=2.0,
                stake_unidades=2.0,
                evento="Azul x Verde",
                quando="2026-09-20T21:00:00-03:00",
                selecoes=[{"mercado": "Resultado final", "escolha": "Azul"}],
                confianca=0.99,
            )
        ]
        self.sent = []
        buffer = BytesIO()
        Image.new("RGB", (2, 2), tuple(uuid4().bytes[:3])).save(buffer, format="PNG")
        self.image = buffer.getvalue()
        self.failure = None
        self.extraction_timeout = False
        self.reads = 0

    def ler(self, *args, **kwargs):
        self.reads += 1
        if self.extraction_timeout:
            raise TimeoutError("SENTINEL-external-image-content")
        return Leitura(tuple(self.coupons), self.modelo)

    def http(self, request):
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photo.jpg"}})
        if request.method == "GET":
            return httpx.Response(200, content=self.image)
        assert request.url.path.endswith("sendMessage")
        if self.failure == "timeout":
            raise httpx.ReadTimeout("SENTINEL-token-content", request=request)
        if self.failure:
            return httpx.Response(self.failure, json={"parameters": {"retry_after": 1}})
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})


class BotSystem:
    def __init__(self, engine, http, provider, client, user, chat, como):
        self.engine, self.http, self.provider, self.client = engine, http, provider, client
        self.user, self.chat, self.como = user, chat, como

    async def send(self, message=None, *, raw=None, repeats=1, process=True):
        identifier = uuid4().int & ((1 << 62) - 1)
        raw = raw or {
            "update_id": identifier,
            "message": {
                "message_id": identifier,
                "from": {"id": self.chat},
                "chat": {"id": self.chat, "type": "private"},
                **(message or {}),
            },
        }
        for _ in range(repeats):
            response = await self.http.post(
                webhook.WEBHOOK_PATH,
                json=raw,
                headers={
                    webhook.SECRET_HEADER: "synthetic-webhook-secret",
                },
            )
            assert response.status_code == 202, response.text
        if process:
            for _ in range(100):
                async with AsyncSession(self.engine) as session:
                    await telegram._transport_scope(session)
                    state = await session.scalar(
                        select(models.TelegramInbox.status).where(
                            models.TelegramInbox.update_id == raw["update_id"]
                        )
                    )
                if state == "DONE":
                    break
                assert await telegram.process_inbox_once(self.engine)
            assert state == "DONE"
        return raw

    async def draft(self):
        async with self.como(self.engine, self.user) as session:
            return await session.scalar(
                select(models.RascunhoAposta)
                .where(models.RascunhoAposta.usuario_id == self.user)
                .order_by(models.RascunhoAposta.created_at.desc())
            )

    async def photo(self, **extra):
        raw = await self.send({"photo": [{"file_id": "synthetic-photo"}], **extra})
        draft = await self.draft()
        assert draft is not None
        assert await telegram_extraction.process_photo_once(
            self.engine, self.client, draft_id=draft.id
        )
        return raw, await self.draft()

    async def replies(self):
        async with self.como(self.engine, self.user) as session:
            return list(
                await session.scalars(
                    select(models.TelegramOutbox)
                    .where(models.TelegramOutbox.usuario_id == self.user)
                    .order_by(models.TelegramOutbox.id)
                )
            )

    async def bets(self):
        async with self.como(self.engine, self.user) as session:
            return list(
                await session.scalars(
                    select(models.Aposta).where(models.Aposta.usuario_id == self.user)
                )
            )

    async def deliver(self):
        for reply in await self.replies():
            await telegram.deliver_outbox_once(self.engine, self.client, outbox_id=reply.id)


@pytest.fixture
async def bot(engine_admin, engine_app, banco, novo_usuario, como, monkeypatch, bot_redis):
    user = await novo_usuario()
    chat = 20_000_000_000 + uuid4().int % 1_000_000_000
    from sqlalchemy.dialects.postgresql import insert

    async with engine_admin.begin() as conn:
        await conn.execute(insert(models.Casa).values(nome="Betano").on_conflict_do_nothing())
        house = await conn.scalar(select(models.Casa.id).where(models.Casa.nome == "Betano"))
    async with como(engine_app, user) as session:
        account = await ContaCasaRepo().create(
            session,
            {
                "usuario_id": user,
                "casa_id": house,
                "estado": "EM_USO",
                "apelido": "principal",
            },
        )
        await UsoContaCasaRepo().open(
            session, user, house, account.id, datetime(2026, 1, 1, tzinfo=UTC)
        )
        code = await issue_code(session, user)
        await session.commit()
    settings = get_settings().model_copy(
        update={
            "TELEGRAM_WEBHOOK_SECRET": "synthetic-webhook-secret",
            "TELEGRAM_MODE": "webhook",
        }
    )
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)

    async def database():
        async with AsyncSession(engine_app) as session:
            yield session

    monkeypatch.setattr(webhook, "get_db_primary", database)
    # NullPool: the real sync extraction creates a fresh asyncio loop in its thread.
    worker_engine = create_async_engine(banco.url_app, poolclass=NullPool)
    monkeypatch.setattr(materialization, "get_engine", lambda: worker_engine)
    provider = FakeProviders()
    monkeypatch.setattr(extraction, "get_leitor", lambda: provider)
    monkeypatch.setattr(extraction, "get_cache", lambda: ExtracaoCache(bot_redis, ttl_segundos=60))
    monkeypatch.setattr(
        extraction, "get_limiter", lambda: AnthropicLimiter(bot_redis, 100, 10000, 600)
    )
    client = TelegramClient("synthetic-bot-token", transport=httpx.MockTransport(provider.http))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        system = BotSystem(engine_app, http, provider, client, user, chat, como)
        await system.send({"text": f"/vincular {code.code}"}, repeats=10)
        yield system
    await client.aclose()
    await worker_engine.dispose()


@pytest.mark.parametrize("missing", [False, True])
async def test_complete_and_missing_field_e2e(bot, missing, bot_redis):
    if missing:
        bot.provider.coupons[0] = bot.provider.coupons[0].model_copy(
            update={"casa": None, "stake_unidades": None}
        )
    raw, draft = await bot.photo(
        forward_origin={"type": "user", "sender_user": {"id": 999}, "date": 1760000000}
    )
    assert not await bot.bets()
    assert bot.provider.reads == 1
    assert bot_redis.exists(f"rl:anthropic:user:{bot.user}")
    if missing:
        assert set(draft.missing_fields_json) == {"casa", "stake_unidades"}
        reply = decrypt_payload((await bot.replies())[-1].payload_ciphertext)["text"]
        assert "odd" in reply.lower() and "foto" in reply.lower()
        await bot.send({"text": "casa=Betano; stake=2"})
        draft = await bot.draft()
    assert draft.status == "AWAITING_CONFIRMATION"
    await bot.send(raw=raw, repeats=10)
    # Fresh engine and HTTP requests recover exclusively persisted state.
    fresh = create_async_engine(bot.engine.url, poolclass=NullPool)
    bot.engine = fresh
    try:
        await bot.send({"text": "/continuar"})
        raw = await bot.send({"text": "/confirmar"}, repeats=10, process=False)
        await asyncio.gather(*(telegram.process_inbox_once(fresh) for _ in range(10)))
        await bot.send(raw=raw)
        bets = await bot.bets()
        assert len(bets) == 1
        bet = bets[0]
        assert (bet.stake_centavos, bet.stake_unidades, bet.odd) == (20000, 2.0, 2.0)
        async with bot.como(fresh, bot.user) as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(models.Evento)
                    .where(
                        models.Evento.usuario_id == bot.user, models.Evento.tipo == "APOSTA_CRIADA"
                    )
                )
                == 1
            )
        await bot.deliver()
        saved = [x["text"] for x in bot.provider.sent if "Aposta registrada" in x["text"]]
        assert len(saved) == 1 and f"#{bet.id}" in saved[0] and "20000 centavos" in saved[0]
    finally:
        await fresh.dispose()


@pytest.mark.parametrize("kind", ["unreadable", "multiple"])
async def test_ambiguity_correction_cancel_and_resume(bot, kind):
    if kind == "unreadable":
        bot.provider.coupons = [ExtracaoBilhete(ilegivel=True)]
    else:
        bot.provider.coupons *= 2
    _, draft = await bot.photo()
    assert draft.status == "AWAITING_INFORMATION"
    await bot.send({"text": "/confirmar"})
    assert not await bot.bets()
    if kind == "multiple":
        await bot.send({"text": "cupom=1"})
    await asyncio.gather(
        bot.send({"text": "/corrigir odd 1,95"}, process=False),
        bot.send({"text": "stake=3"}, process=False),
    )
    await asyncio.gather(*(telegram.process_inbox_once(bot.engine) for _ in range(2)))
    draft = await bot.draft()
    assert (
        draft.fields_json["odd"] == pytest.approx(1.95) and draft.fields_json["stake_unidades"] == 3
    )
    await bot.send({"text": "/continuar"})
    await bot.send({"text": "/cancelar"})
    await bot.send({"text": "/confirmar"})
    assert (await bot.draft()).status == "CANCELLED" and not await bot.bets()


@pytest.mark.parametrize("failure", [500, 429, "timeout"])
async def test_delivery_outage_crash_and_recovery(bot, failure):
    await bot.photo()
    await bot.send({"text": "/confirmar"})
    bot.provider.failure = failure
    await bot.deliver()
    replies = await bot.replies()
    assert all(row.status == "PENDING" for row in replies)
    async with bot.como(bot.engine, bot.user) as session:
        await session.execute(
            update(models.TelegramOutbox)
            .where(models.TelegramOutbox.usuario_id == bot.user)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    claim = await telegram._claim_outbox(bot.engine, outbox_id=replies[-1].id)
    assert claim
    async with bot.como(bot.engine, bot.user) as session:
        await session.execute(
            update(models.TelegramOutbox)
            .where(models.TelegramOutbox.id == claim.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    bot.provider.failure = None
    await bot.deliver()
    assert all(row.status == "SENT" for row in await bot.replies())
    assert len(await bot.bets()) == 1
    assert len(bot.provider.sent) == len(replies)


async def test_extraction_timeout_and_abandoned_lease_recover(bot):
    bot.provider.extraction_timeout = True
    _, draft = await bot.photo()
    assert draft.extraction_completed_at is None and not await bot.bets()
    async with bot.como(bot.engine, bot.user) as session:
        await session.execute(
            update(models.RascunhoAposta)
            .where(models.RascunhoAposta.id == draft.id)
            .values(extraction_next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    claim = await telegram_extraction.claim_photo(bot.engine, draft_id=draft.id)
    assert claim
    async with bot.como(bot.engine, bot.user) as session:
        await session.execute(
            update(models.RascunhoAposta)
            .where(models.RascunhoAposta.id == draft.id)
            .values(extraction_lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    bot.provider.extraction_timeout = False
    assert await telegram_extraction.process_photo_once(bot.engine, bot.client, draft_id=draft.id)
    await bot.send({"text": "/confirmar"})
    assert len(await bot.bets()) == 1


@pytest.mark.parametrize("action", ["link", "photo", "correction", "confirmation"])
async def test_shared_limits_atomic_expiry_and_tenant_separation(bot, monkeypatch, action):
    from bancaemdia.services import telegram_abuse

    settings = get_settings().model_copy(
        update={
            "TELEGRAM_ACTION_LIMITS": dict.fromkeys(
                ("link", "photo", "correction", "confirmation"), (2, 2, 10000)
            )
        }
    )
    monkeypatch.setattr(telegram_abuse, "get_settings", lambda: settings)

    async def attempt(sender, owner):
        async with AsyncSession(bot.engine) as session, session.begin():
            await telegram._transport_scope(session)
            return await admit(session, action=action, sender=sender, chat=sender, owner=owner)

    # Use a fresh identity: setup has already consumed one link attempt.
    owner = bot.user + 10000000
    results = await asyncio.gather(*(attempt(bot.chat + 1, owner) for _ in range(12)))
    assert sum(results) == 2
    assert await attempt(bot.chat + 2, owner + 1)
    async with AsyncSession(bot.engine) as session, session.begin():
        await telegram._transport_scope(session)
        await session.execute(
            text(
                "UPDATE telegram_rate_buckets SET expires_at=clock_timestamp()-interval '1 second'"
            )
        )
    assert await attempt(bot.chat + 1, owner)


@pytest.mark.parametrize("scope", ["chat", "user", "global"])
async def test_each_quota_dimension_is_enforced(bot, monkeypatch, scope):
    from bancaemdia.services import telegram_abuse
    from bancaemdia.services.telegram_link import _digest

    limits = {"chat": (1, 1000, 1000), "user": (1000, 1, 1000), "global": (1000, 1000, 1)}[scope]
    settings = get_settings().model_copy(
        update={
            "TELEGRAM_ACTION_LIMITS": dict.fromkeys(
                ("link", "photo", "correction", "confirmation"), limits
            )
        }
    )
    monkeypatch.setattr(telegram_abuse, "get_settings", lambda: settings)
    async with AsyncSession(bot.engine) as session, session.begin():
        await telegram._transport_scope(session)
        await session.execute(
            text("DELETE FROM telegram_rate_buckets WHERE key=:key"),
            {"key": _digest("quota", "photo:global")},
        )

    async def attempt(chat, owner):
        async with AsyncSession(bot.engine) as session, session.begin():
            await telegram._transport_scope(session)
            return await admit(session, action="photo", sender=chat, chat=chat, owner=owner)

    assert await attempt(bot.chat, bot.user)
    assert not await attempt(
        bot.chat if scope == "chat" else bot.chat + 1,
        bot.user if scope == "user" else bot.user + 1000000,
    )


async def test_legacy_photo_rls_and_purge(bot, engine_admin, novo_usuario):
    _, draft = await bot.photo()
    other = await novo_usuario()
    async with engine_admin.begin() as conn:
        await conn.execute(
            text("INSERT INTO midias(hash,tipo,bytes) VALUES (:hash,'image/png',10)"),
            {"hash": draft.media_hash},
        )
        await conn.execute(
            text("INSERT INTO midia_arquivos(hash,conteudo) VALUES (:hash,:content)"),
            {"hash": draft.media_hash, "content": b"SENTINEL-legacy-photo"},
        )
    for user in (None, other):
        async with bot.como(bot.engine, user) as session:
            for model in (models.Midia, models.MidiaArquivo):
                assert (
                    await session.scalar(select(model).where(model.hash == draft.media_hash))
                    is None
                )
    async with bot.como(bot.engine, bot.user) as session:
        assert (
            await session.scalar(
                select(models.MidiaArquivo).where(models.MidiaArquivo.hash == draft.media_hash)
            )
            is not None
        )
    await bot.send({"text": "/cancelar"})
    async with bot.como(bot.engine, bot.user) as session:
        await session.execute(
            update(models.RascunhoAposta)
            .where(models.RascunhoAposta.id == draft.id)
            .values(closed_at=datetime.now(UTC) - timedelta(days=40))
        )
        await session.commit()
    await purge_telegram(bot.engine)
    async with engine_admin.connect() as conn:
        assert (
            await conn.scalar(
                select(models.MidiaArquivo.id).where(models.MidiaArquivo.hash == draft.media_hash)
            )
            is None
        )


async def test_retention_preserves_pending_work_and_replay_tombstones(bot):
    raw, draft = await bot.photo()
    await bot.send({"text": "/confirmar"})
    await bot.deliver()
    old = datetime.now(UTC) - timedelta(days=40)
    async with bot.como(bot.engine, bot.user) as session:
        await session.execute(
            update(models.RascunhoAposta)
            .where(models.RascunhoAposta.id == draft.id)
            .values(closed_at=old)
        )
        await session.execute(
            update(models.TelegramOutbox)
            .where(models.TelegramOutbox.usuario_id == bot.user)
            .values(created_at=old)
        )
        await session.commit()
    await asyncio.gather(purge_telegram(bot.engine), purge_telegram(bot.engine))
    await purge_telegram(bot.engine)
    async with bot.como(bot.engine, bot.user) as session:
        stored = await session.get(models.RascunhoAposta, draft.id)
        assert stored.fields_json == {} and stored.media_reference_ciphertext is None
        assert stored.telegram_chat_id == 0 and stored.origin_digest
        assert await session.get(models.TelegramMedia, draft.id) is None
        assert all(r.payload_ciphertext == b"" for r in await bot.replies())
    # A replay with a different update_id but the original message_id remains a tombstone.
    raw["update_id"] += 1
    await bot.send(raw=raw)
    assert len(await bot.bets()) == 1
    await bot.send({"photo": [{"file_id": "pending"}]})
    pending = await bot.draft()
    async with bot.como(bot.engine, bot.user) as session:
        await session.execute(
            update(models.RascunhoAposta)
            .where(models.RascunhoAposta.id == pending.id)
            .values(created_at=old, updated_at=old)
        )
        await session.commit()
    await purge_telegram(bot.engine)
    async with bot.como(bot.engine, bot.user) as session:
        assert (
            await session.get(models.RascunhoAposta, pending.id)
        ).media_reference_ciphertext is not None


async def test_restricted_rls_blocks_valid_foreign_ids(bot, novo_usuario):
    _, draft = await bot.photo()
    await bot.send({"text": "/confirmar"})
    other = await novo_usuario()
    async with bot.como(bot.engine, other) as session:
        assert not await session.scalar(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
        )
        for model in (
            models.TelegramLink,
            models.RascunhoAposta,
            models.TelegramOutbox,
            models.TelegramMedia,
            models.Aposta,
        ):
            assert (
                await session.scalar(
                    select(func.count()).select_from(model).where(model.usuario_id == bot.user)
                )
                == 0
            )
        assert await session.get(models.RascunhoAposta, draft.id) is None
        assert await session.get(models.TelegramMedia, draft.id) is None
        assert await session.scalar(select(func.count()).select_from(models.TelegramInbox)) == 0


@pytest.mark.parametrize(
    "kind", ["group", "channel", "unlinked", "album", "text", "callback", "edited"]
)
async def test_trust_boundaries_have_no_financial_or_draft_effect(bot, kind):
    raw = {
        "update_id": uuid4().int & ((1 << 62) - 1),
        "message": {
            "message_id": 77,
            "from": {"id": bot.chat},
            "chat": {"id": bot.chat, "type": "private"},
            "photo": [{"file_id": "one"}],
        },
    }
    message = raw["message"]
    if kind in {"group", "channel"}:
        message["chat"]["type"] = kind
    elif kind == "unlinked":
        message["from"]["id"] += 1
    elif kind == "album":
        message["media_group_id"] = "album"
    elif kind == "text":
        del message["photo"]
        message["text"] = "casa=Betano; odd=2; stake=2"
    elif kind == "callback":
        raw["callback_query"] = {
            "from": message["from"],
            "message": message,
            "data": "confirm:old:foreign:expired",
        }
        del raw["message"]
    elif kind == "edited":
        raw["edited_message"] = raw.pop("message")
    await bot.send(raw=raw, repeats=10)
    assert await bot.draft() is None and not await bot.bets()


@pytest.mark.parametrize(
    "content",
    [b"not an image", b"\x89PNG\r\n\x1a\ninvalid", b"", b"x" * (20 * 1024 * 1024 + 1)],
    ids=["text", "corrupt-image", "empty", "over-20-mib"],
)
async def test_invalid_media_cannot_be_confirmed(bot, content):
    bot.provider.image = content
    _, draft = await bot.photo()
    assert draft.status == "FAILED" and draft.media_reference_ciphertext is None
    assert bot.provider.reads == 0
    await bot.send({"text": "/confirmar"})
    assert not await bot.bets()
    async with bot.como(bot.engine, bot.user) as session:
        assert await session.get(models.TelegramMedia, draft.id) is None


async def test_http_rejections_do_not_enter_durable_inbox(bot):
    async with AsyncSession(bot.engine) as session:
        await telegram._transport_scope(session)
        before = await session.scalar(select(func.count()).select_from(models.TelegramInbox))
    headers = {
        webhook.SECRET_HEADER: "synthetic-webhook-secret",
        "content-type": "application/json",
    }
    for content, offered, status in (
        (b"{}", {**headers, webhook.SECRET_HEADER: "forged-SENTINEL"}, 403),
        (b"{}", {**headers, "content-type": "image/jpeg"}, 415),
        (b"x" * (128 * 1024 + 1), headers, 413),
        (b"not-json-SENTINEL", headers, 400),
        (b'{"update_id":true}', headers, 400),
    ):
        response = await bot.http.post(webhook.WEBHOOK_PATH, content=content, headers=offered)
        assert response.status_code == status and "SENTINEL" not in response.text
    async with AsyncSession(bot.engine) as session:
        await telegram._transport_scope(session)
        assert (
            await session.scalar(select(func.count()).select_from(models.TelegramInbox)) == before
        )


async def test_unlink_cancels_draft_and_stops_claimed_extraction_and_delivery(bot):
    from bancaemdia.services.telegram_link import revoke_link

    await bot.send({"photo": [{"file_id": "one"}]})
    draft = await bot.draft()
    claim = await telegram_extraction.claim_photo(bot.engine, draft_id=draft.id)
    assert claim
    await bot.deliver()
    assert bot.provider.sent
    bot.provider.sent.clear()
    async with bot.como(bot.engine, bot.user) as session:
        assert await revoke_link(session, bot.user)
        await session.commit()
    await telegram_extraction._complete(
        bot.engine, claim, content=b"private", mime="image/jpeg", reading={}
    )
    await bot.send({"text": "/confirmar"})
    await bot.deliver()
    assert bot.provider.sent == [] and not await bot.bets()
    assert (await bot.draft()).status == "CANCELLED"
    async with bot.como(bot.engine, bot.user) as session:
        assert await session.get(models.TelegramMedia, draft.id) is None
        responses = (await session.scalars(select(models.TelegramOutbox))).all()
        assert responses
        assert all(
            row.chat_id == 0 and row.telegram_message_id is None and not row.payload_ciphertext
            for row in responses
        )


async def test_limiter_backend_failure_rolls_back_and_preserves_update(bot, monkeypatch):
    original = telegram.admit

    async def failed(*args, **kwargs):
        await asyncio.sleep(0)
        raise ConnectionError("SENTINEL-limit-backend")

    monkeypatch.setattr(telegram, "admit", failed)
    raw = await bot.send({"photo": [{"file_id": "one"}]}, process=False)
    assert await telegram.process_inbox_once(bot.engine)
    assert await bot.draft() is None
    async with AsyncSession(bot.engine) as session, session.begin():
        await telegram._transport_scope(session)
        item = await session.scalar(
            select(models.TelegramInbox).where(models.TelegramInbox.update_id == raw["update_id"])
        )
        assert (
            item.status == "PENDING"
            and item.payload_ciphertext
            and item.last_error_code == "processing"
        )
        item.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(telegram, "admit", original)
    await bot.send(raw=raw, repeats=10)
    assert (await bot.draft()).status == "AWAITING_EXTRACTION"


async def test_financial_rollback_retries_one_event_and_one_bet(bot, monkeypatch):
    from bancaemdia.services import telegram_confirmation

    original = telegram_confirmation._queue_success

    async def failed(*args, **kwargs):
        await asyncio.sleep(0)
        raise RuntimeError("SENTINEL-after-financial-write")

    await bot.photo()
    monkeypatch.setattr(telegram_confirmation, "_queue_success", failed)
    raw = await bot.send({"text": "/confirmar"}, process=False)
    assert await telegram.process_inbox_once(bot.engine)
    assert not await bot.bets()
    async with AsyncSession(bot.engine) as session, session.begin():
        await telegram._transport_scope(session)
        await session.execute(
            update(models.TelegramInbox)
            .where(models.TelegramInbox.update_id == raw["update_id"])
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    monkeypatch.setattr(telegram_confirmation, "_queue_success", original)
    await bot.send(raw=raw)
    assert len(await bot.bets()) == 1


async def test_private_cache_and_transport_diagnostics_do_not_leak(bot, bot_redis, caplog):
    from prometheus_client import generate_latest

    from bancaemdia.cache.telegram_cache import TelegramExtractionCache
    from bancaemdia.services.telegram_link import _digest

    bot.provider.coupons[0] = bot.provider.coupons[0].model_copy(
        update={"evento": "SENTINEL-private-event"}
    )
    await bot.photo(caption="SENTINEL-private-caption")
    bot.provider.failure = "timeout"
    await bot.deliver()
    observed = caplog.text + generate_latest().decode()
    assert "SENTINEL" not in observed and "synthetic-bot-token" not in observed
    prefix = "tgext:" + _digest("cache", str(bot.user)) + ":"
    keys = list(bot_redis.scan_iter(match=prefix + "*"))
    assert keys
    for key in keys:
        assert 0 < bot_redis.ttl(key) <= 60
        assert b"SENTINEL" not in bot_redis.get(key)
    other_cache = TelegramExtractionCache(ExtracaoCache(bot_redis), bot.user + 1000000)
    assert other_cache.buscar(keys[0].decode().split(":")[2]) is None


async def test_subscription_denial_preserves_cancellation_and_purge(bot, engine_admin):
    _, draft = await bot.photo()
    async with engine_admin.begin() as conn:
        await conn.execute(
            text("UPDATE billing_rollout SET activated_at=clock_timestamp() WHERE id=1")
        )
    try:
        await bot.send({"text": "/confirmar"})
        assert not await bot.bets()
        assert (
            "modo de leitura"
            in decrypt_payload((await bot.replies())[-1].payload_ciphertext)["text"]
        )
        await bot.send({"text": "/cancelar"})
        assert (await bot.draft()).status == "CANCELLED"
        async with engine_admin.begin() as conn:
            # Administrative time travel, with no weakening of the application role.
            await conn.execute(text("UPDATE billing_rollout SET activated_at=NULL WHERE id=1"))
            await conn.execute(
                update(models.RascunhoAposta)
                .where(models.RascunhoAposta.id == draft.id)
                .values(closed_at=datetime.now(UTC) - timedelta(days=40))
            )
            await conn.execute(
                text("UPDATE billing_rollout SET activated_at=clock_timestamp() WHERE id=1")
            )
        await purge_telegram(bot.engine)
        async with bot.como(bot.engine, bot.user) as session:
            stored = await session.get(models.RascunhoAposta, draft.id)
            assert stored.fields_json == {} and stored.purged_at
        from sqlalchemy.exc import DBAPIError

        with pytest.raises(DBAPIError) as error:
            async with bot.como(bot.engine, bot.user) as session:
                await session.execute(
                    update(models.RascunhoAposta)
                    .where(models.RascunhoAposta.id == draft.id)
                    .values(fields_json={"stake_unidades": 999})
                )
        assert getattr(error.value.orig, "sqlstate", None) == "P0402"
    finally:
        async with engine_admin.begin() as conn:
            await conn.execute(text("UPDATE billing_rollout SET activated_at=NULL WHERE id=1"))


async def test_expired_photo_pauses_without_provider_and_resumes_after_payment(bot, engine_admin):
    await bot.send({"photo": [{"file_id": "pending-before-expiry"}]})
    draft = await bot.draft()
    assert draft.status == "AWAITING_EXTRACTION"
    original_fields = draft.fields_json
    reference = draft.media_reference_ciphertext
    async with engine_admin.begin() as conn:
        await conn.execute(text("SELECT billing_activate_rollout()"))
        expired = await conn.execute(
            text(
                "UPDATE assinaturas SET trial_confirmed=false, status='EXPIRED' WHERE usuario_id=:uid"
            ),
            {"uid": bot.user},
        )
        assert expired.rowcount == 1
    try:
        assert not await telegram_extraction.process_photo_once(
            bot.engine, bot.client, draft_id=draft.id
        )
        paused = await bot.draft()
        assert paused.status == "AWAITING_EXTRACTION"
        assert paused.fields_json == original_fields
        assert paused.media_reference_ciphertext == reference
        assert paused.extraction_error_code == "account_read_only"
        assert paused.extraction_lease_token is None and paused.extraction_lease_until is None
        assert bot.provider.reads == 0 and not await bot.bets()
        async with engine_admin.begin() as conn:
            price = await conn.scalar(
                text("""INSERT INTO billing_prices
                (amount_cents,currency,frequency,valid_from,published)
                VALUES (12345,'BRL','MONTHLY',clock_timestamp(),false) RETURNING id""")
            )
            await conn.execute(
                text("""UPDATE assinaturas SET status='ACTIVE',
                    price_id=:price,
                    current_period_started_at=clock_timestamp()-interval '1 hour',
                    current_period_ends_at=clock_timestamp()+interval '1 day' WHERE usuario_id=:uid"""),
                {"uid": bot.user, "price": price},
            )
            await conn.execute(
                update(models.RascunhoAposta)
                .where(models.RascunhoAposta.id == draft.id)
                .values(extraction_next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        assert await telegram_extraction.process_photo_once(
            bot.engine, bot.client, draft_id=draft.id
        )
        assert bot.provider.reads == 1
        assert (await bot.draft()).status == "AWAITING_CONFIRMATION"
        assert not await bot.bets()
        await bot.send({"text": "/confirmar"})
        assert len(await bot.bets()) == 1
    finally:
        async with engine_admin.begin() as conn:
            await conn.execute(text("UPDATE billing_rollout SET activated_at=NULL WHERE id=1"))


async def test_real_redis_celery_tick_consumes_durable_inbox(bot, banco, bot_redis, monkeypatch):
    from celery import Celery
    from celery.contrib.testing.worker import start_worker

    connection = bot_redis.connection_pool.connection_kwargs
    url = f"redis://{connection['host']}:{connection['port']}/0"
    worker_engine = create_async_engine(banco.url_app, poolclass=NullPool)
    monkeypatch.setattr(telegram, "get_engine", lambda: worker_engine)
    monkeypatch.setattr(
        telegram,
        "TelegramClient",
        lambda: TelegramClient("synthetic-token", transport=httpx.MockTransport(bot.provider.http)),
    )
    worker_app = Celery("telegram-hardening", broker=url)
    worker_app.conf.update(
        broker_transport_options={"global_keyprefix": uuid4().hex}, task_ignore_result=True
    )
    worker_app.task(name="telegram.tick")(telegram.telegram_tick)
    raw = await bot.send({"photo": [{"file_id": "queued"}]}, process=False)
    try:
        with start_worker(worker_app, pool="solo", perform_ping_check=False, loglevel="ERROR"):
            worker_app.send_task("telegram.tick")
            for _ in range(100):
                draft = await bot.draft()
                if draft and draft.extraction_completed_at:
                    break
                await asyncio.sleep(0.1)
            assert draft and draft.extraction_completed_at
            assert draft.telegram_update_id == raw["update_id"]
            assert not await bot.bets()
    finally:
        worker_app.close()
        await worker_engine.dispose()


async def test_migration_roundtrip_preserves_prerequisite_draft(engine_admin):
    import subprocess
    import sys

    from sqlalchemy.engine import make_url

    name = "tg102_" + uuid4().hex
    async with engine_admin.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(engine_admin.url).set(database=name).render_as_string(hide_password=False)
    isolated = create_async_engine(url, poolclass=NullPool)
    env = {**os.environ, "DATABASE_URL": url, "DATABASE_URL_REPLICA": url}

    async def migrate(*args, success=True):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            env=env,
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) == success, result.stderr

    try:
        await migrate("upgrade", "head")
        identifier = uuid4()
        async with isolated.begin() as conn:
            user = await conn.scalar(
                text(
                    "INSERT INTO usuarios(email,nome) VALUES ('migration@synthetic.invalid','Synthetic') RETURNING id"
                )
            )
            await conn.execute(
                text("""INSERT INTO rascunhos_aposta
                (id,usuario_id,telegram_chat_id,telegram_message_id,telegram_update_id)
                VALUES (:id,:user,123,456,789)"""),
                {"id": identifier, "user": user},
            )
            await conn.execute(
                text("""INSERT INTO telegram_media
                (draft_id,usuario_id,content_ciphertext,content_hash,mime)
                VALUES (:id,:user,:content,:hash,'image/png')"""),
                {"id": identifier, "user": user, "content": b"encrypted-fixture", "hash": "a" * 64},
            )
        await migrate("downgrade", "f141chain2026", success=False)
        async with isolated.begin() as conn:
            await conn.execute(text("DELETE FROM telegram_media"))
        await migrate("downgrade", "f141chain2026")
        await migrate("upgrade", "head")
        async with isolated.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT telegram_update_id FROM rascunhos_aposta WHERE id=:id"),
                    {"id": identifier},
                )
                == 789
            )
    finally:
        await isolated.dispose()
        async with engine_admin.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"DROP DATABASE {name}"))
