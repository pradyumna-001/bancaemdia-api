"""Transactional admission and original-photo provenance, scoped by the tenant."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.config import get_settings
from bancaemdia.domain.foto_aposta import StoredPhoto
from bancaemdia.domain.web_print import (
    MAX_INTENT_BYTES,
    MAX_ITEMS,
    MEDIA_DAYS,
    STATE_DAYS,
    PrintItemResponse,
)
from bancaemdia.models.web_print import PrintAsset, PrintIntent, PrintItem


def cipher() -> Fernet:
    secret = get_settings().COLETA_TOKEN_SECRET
    if not secret:
        raise HTTPException(
            503, "o armazenamento privado está indisponível", headers={"Retry-After": "5"}
        )
    key = hashlib.sha256(b"web-print-original:v1:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def now() -> datetime:
    return datetime.now(UTC)


async def owner(session: AsyncSession, uid: int) -> None:
    await session.execute(
        text("SELECT set_config('app.current_user_id',:uid,true)"), {"uid": str(uid)}
    )


async def lock(session: AsyncSession, uid: int) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": f"web-prints:{uid}"}
    )


async def find(
    session: AsyncSession, uid: int, intent: UUID, item: UUID
) -> tuple[PrintIntent, PrintItem, PrintAsset]:
    row = (
        await session.execute(
            select(PrintIntent, PrintItem, PrintAsset)
            .join(
                PrintItem,
                (PrintItem.usuario_id == PrintIntent.usuario_id)
                & (PrintItem.intent_id == PrintIntent.id),
            )
            .join(
                PrintAsset,
                (PrintAsset.usuario_id == PrintItem.usuario_id)
                & (PrintAsset.id == PrintItem.asset_id),
            )
            .where(
                PrintIntent.usuario_id == uid,
                PrintIntent.id == intent,
                PrintItem.id == item,
                PrintIntent.expires_at > func.clock_timestamp(),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "não achei este arquivo")
    return row[0], row[1], row[2]


def response(intent: PrintIntent, item: PrintItem, asset: PrintAsset) -> PrintItemResponse:
    outcome = None
    if asset.status == "completed":
        outcome = (
            "duplicate"
            if item.duplicate
            else (
                "review"
                if any(r["type"] == "review" for r in asset.results or [])
                else "materialized"
                if asset.results
                else "no_bet"
            )
        )
    elif asset.status == "failed":
        outcome = "failed"
    return PrintItemResponse.model_validate({
        "intent_id": intent.id,
        "item_id": item.id,
        "status": asset.status,
        "outcome": outcome,
        "duplicate": item.duplicate,
        "attempt": asset.attempt,
        "last_retry_id": asset.retry_token,
        "retryable": asset.status == "failed"
        and asset.retryable
        and asset.attempt < 3
        and asset.media_expires_at > now(),
        "error_code": asset.error_code,
        "progress_percent": 100 if asset.status in {"completed", "failed"} else None,
        "results": asset.results,
        "media_expires_at": asset.media_expires_at,
        "reconciliation_expires_at": intent.expires_at,
    })


async def accept(
    session: AsyncSession, uid: int, intent_id: UUID, item_id: UUID, content: bytes, mime: str
) -> PrintItemResponse:
    await owner(session, uid)
    await lock(session, uid)
    intent = await session.get(PrintIntent, (uid, intent_id))
    if intent is not None and intent.expires_at <= now():
        raise HTTPException(410, "esta intenção expirou; não reenvie automaticamente")
    digest = hashlib.sha256(content).hexdigest()
    existing = await session.get(PrintItem, (uid, intent_id, item_id))
    if existing is not None:
        asset = await session.get(PrintAsset, existing.asset_id)
        assert asset is not None and intent is not None
        if asset.digest != digest or asset.size != len(content):
            raise HTTPException(409, "esta identidade já está ligada a outro arquivo")
        return response(intent, existing, asset)
    items = list(
        (
            await session.scalars(
                select(PrintItem).where(
                    PrintItem.usuario_id == uid, PrintItem.intent_id == intent_id
                )
            )
        ).all()
    )
    total = await session.scalar(
        select(func.coalesce(func.sum(PrintAsset.size), 0))
        .join(PrintItem, PrintItem.asset_id == PrintAsset.id)
        .where(PrintItem.usuario_id == uid, PrintItem.intent_id == intent_id)
    )
    if len(items) >= MAX_ITEMS or int(total or 0) + len(content) > MAX_INTENT_BYTES:
        raise HTTPException(413, "esta intenção passou do limite de arquivos ou bytes")
    asset = await session.scalar(
        select(PrintAsset).where(PrintAsset.usuario_id == uid, PrintAsset.digest == digest)
    )
    duplicate = asset is not None
    if asset is None:
        await reserve_attempt(session, uid)
        asset = PrintAsset(
            id=uuid4(),
            usuario_id=uid,
            digest=digest,
            mime=mime,
            size=len(content),
            ciphertext=cipher().encrypt(content),
            media_expires_at=now() + timedelta(days=MEDIA_DAYS),
            status="accepted",
            attempt=1,
            retryable=False,
        )
        session.add(asset)
    elif asset.results is None and asset.criado_em + timedelta(days=STATE_DAYS) <= now():
        raise HTTPException(410, "este arquivo já foi recebido; o resultado expirou")
    if intent is None:
        intent = PrintIntent(
            usuario_id=uid, id=intent_id, expires_at=now() + timedelta(days=STATE_DAYS)
        )
        session.add(intent)
    await session.flush()
    item = PrintItem(
        usuario_id=uid, intent_id=intent_id, id=item_id, asset_id=asset.id, duplicate=duplicate
    )
    session.add(item)
    await session.flush()
    return response(intent, item, asset)


async def reserve_attempt(session: AsyncSession, uid: int, asset: PrintAsset | None = None) -> None:
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    used = await session.scalar(
        select(func.coalesce(func.sum(PrintAsset.attempt), 0)).where(
            PrintAsset.usuario_id == uid, PrintAsset.quota_day >= start
        )
    )
    # Today's sum already includes this asset: reserve only the new attempt.
    # An older asset carries its lifetime attempts into today's sum conservatively.
    additional = 1 if asset is None or asset.quota_day >= start else asset.attempt + 1
    if int(used or 0) + additional > get_settings().PRINT_DAILY_ATTEMPT_LIMIT:
        raise HTTPException(
            429,
            "o limite interno de arquivos e tentativas foi atingido",
            headers={
                "Retry-After": str(max(1, int((start + timedelta(days=1) - now()).total_seconds())))
            },
        )


async def photo(session: AsyncSession, uid: int, asset_id: UUID) -> StoredPhoto | None:
    asset = await session.scalar(
        select(PrintAsset).where(
            PrintAsset.usuario_id == uid,
            PrintAsset.id == asset_id,
            PrintAsset.media_expires_at > func.clock_timestamp(),
            PrintAsset.ciphertext.is_not(None),
        )
    )
    if asset is None:
        return None
    assert asset.ciphertext is not None
    return StoredPhoto(asset.digest, asset.mime, cipher().decrypt(asset.ciphertext))
