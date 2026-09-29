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
        extraction, "get_limiter", lambda: AnthropicLimiter(bot_redis, 1000, 10000, 1)
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
    assert (await bot.draft()).media_reference_ciphertext is not None


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
