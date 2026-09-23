"""Authenticated Telegram webhook and the shared durable ingress contract."""

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import ErrorResponse
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db_primary
from bancaemdia.integrations.telegram.codec import InvalidTelegramUpdateError, normalize_update
from bancaemdia.models import TelegramInbox
from bancaemdia.observability.metrics import telegram_webhook_rejections_total

WEBHOOK_PATH = "/api/v1/integrations/telegram/webhook"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_WEBHOOK_BYTES = 128 * 1024

router = APIRouter()


class TelegramWebhookAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool


def _reject(status: int, reason: str) -> JSONResponse:
    telegram_webhook_rejections_total.labels(reason=reason).inc()
    return JSONResponse({"detail": "Telegram update rejected"}, status_code=status)


async def ingest_update(session: AsyncSession, raw: object) -> bool:
    """Insert once by update_id. The caller commits before confirming delivery."""
    update = normalize_update(raw)
    await session.execute(text("SELECT set_config('app.telegram_transport', 'on', true)"))
    result = await session.execute(
        insert(TelegramInbox)
        .values(
            update_id=update.update_id,
            event_type=update.event_type,
            sender_user_id=update.sender_user_id,
            chat_id=update.chat_id,
            message_id=update.message_id,
            payload_ciphertext=update.encrypted_payload,
        )
        .on_conflict_do_nothing(index_elements=[TelegramInbox.update_id])
        .returning(TelegramInbox.id)
    )
    return result.scalar_one_or_none() is not None


@router.post(
    WEBHOOK_PATH,
    status_code=202,
    response_model=TelegramWebhookAck,
    summary="Receber atualização do bot Telegram",
    description="Valida o segredo e grava a atualização antes de confirmar o recebimento.",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid Telegram update."},
        403: {"model": ErrorResponse, "description": "Invalid webhook secret."},
        413: {"model": ErrorResponse, "description": "Update body exceeds 128 KiB."},
        415: {"model": ErrorResponse, "description": "JSON content type is required."},
        429: {"model": ErrorResponse, "description": "Webhook request limit exceeded."},
        503: {"model": ErrorResponse, "description": "Webhook is not configured."},
    },
)
async def telegram_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if settings.TELEGRAM_MODE != "webhook" or not settings.TELEGRAM_WEBHOOK_SECRET:
        return _reject(503, "disabled")
    offered = request.headers.get(SECRET_HEADER, "")
    expected = hashlib.sha256(settings.TELEGRAM_WEBHOOK_SECRET.encode()).digest()
    actual = hashlib.sha256(offered.encode()).digest()
    if not hmac.compare_digest(actual, expected):
        return _reject(403, "secret")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        return _reject(415, "content_type")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        return _reject(413, "size")
    try:
        raw: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reject(400, "json")
    try:
        async for session in get_db_primary():
            await ingest_update(session, raw)
            await session.commit()
    except InvalidTelegramUpdateError:
        return _reject(400, "shape")
    return JSONResponse({"accepted": True}, status_code=202, headers={"Cache-Control": "no-store"})
