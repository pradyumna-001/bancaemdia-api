"""Lease-backed Telegram photo reading; only drafts and outbox replies are written."""

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia.extracao.cliente import VERSAO_PROMPT
from bancaemdia.integrations.telegram.client import TelegramApiError, TelegramClient
from bancaemdia.integrations.telegram.codec import InvalidTelegramUpdateError, decrypt_payload
from bancaemdia.integrations.telegram.media import download_photo
from bancaemdia.models.rascunho_aposta import ACTIVE_DRAFT_STATUSES, RascunhoAposta
from bancaemdia.observability.metrics import observe_stage
from bancaemdia.repositories.mensagem_repo import MidiaArquivoRepo, MidiaRepo
from bancaemdia.services.telegram_conversation import apply_extraction, draft_summary
from bancaemdia.services.telegram_photo_intake import candidates_from_reading
from bancaemdia.workers.extraction import RETRY_ON, extrair_bilhete
from bancaemdia.workers.materialization import _set_current_user
from bancaemdia.workers.telegram import queue_reply

MAX_ATTEMPTS = 5
LEASE_SECONDS = 600


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PhotoClaim:
    id: UUID
    user_id: int
    chat_id: int
    message_id: int
    update_id: int
    reference: bytes | None
    attempts: int
    token: str


async def claim_photo(engine: AsyncEngine, *, draft_id: UUID | None = None) -> PhotoClaim | None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.telegram_transport', 'on', true)"))
            due = (
                await session.execute(
                    select(RascunhoAposta.id, RascunhoAposta.usuario_id)
                    .where(
                        RascunhoAposta.status.in_(ACTIVE_DRAFT_STATUSES),
                        or_(
                            RascunhoAposta.status == "AWAITING_EXTRACTION",
                            RascunhoAposta.source_metadata_json.has_key("update_id"),
                        ),
                        RascunhoAposta.extraction_completed_at.is_(None),
                        RascunhoAposta.media_reference_ciphertext.is_not(None),
                        *([RascunhoAposta.id == draft_id] if draft_id is not None else []),
                        RascunhoAposta.extraction_next_attempt_at <= func.clock_timestamp(),
                        or_(
                            RascunhoAposta.extraction_lease_until.is_(None),
                            RascunhoAposta.extraction_lease_until <= func.clock_timestamp(),
                        ),
                    )
                    .order_by(RascunhoAposta.extraction_next_attempt_at, RascunhoAposta.id)
                    .limit(1)
                )
            ).first()
            if due is None:
                return None
            await _set_current_user(session, due.usuario_id)
            draft = await session.scalar(
                select(RascunhoAposta)
                .where(RascunhoAposta.id == due.id)
                .with_for_update(skip_locked=True)
            )
            if (
                draft is None
                or draft.status not in ACTIVE_DRAFT_STATUSES
                or draft.extraction_completed_at is not None
                or draft.extraction_next_attempt_at > _now()
                or (
                    draft.extraction_lease_until is not None
                    and draft.extraction_lease_until > _now()
                )
            ):
                return None
            token = uuid4().hex
            draft.extraction_attempts += 1
            draft.extraction_lease_token = token
            draft.extraction_lease_until = _now() + timedelta(seconds=LEASE_SECONDS)
            return PhotoClaim(
                draft.id,
                draft.usuario_id,
                draft.telegram_chat_id,
                draft.telegram_message_id,
                draft.telegram_update_id,
                draft.media_reference_ciphertext,
                draft.extraction_attempts,
                token,
            )


async def read_photo(
    claim: PhotoClaim, client: TelegramClient
) -> tuple[bytes, str, dict[str, object]]:
    if claim.reference is None:
        raise TelegramApiError("missing_file_reference", retryable=False)
    reference = decrypt_payload(claim.reference)
    file_id = reference.get("file_id")
    if not isinstance(file_id, str):
        raise TelegramApiError("missing_file_reference", retryable=False)
    content, mime = await download_photo(client, file_id)
    filename = {
        "image/jpeg": "photo.jpg",
        "image/png": "photo.png",
        "image/gif": "photo.gif",
        "image/webp": "photo.webp",
    }[mime]
    caption = reference.get("caption")
    reading = await asyncio.to_thread(
        extrair_bilhete,
        claim.user_id,
        base64.b64encode(content).decode("ascii"),
        nome_do_arquivo=filename,
        legenda=caption if isinstance(caption, str) else "",
        chat_id=claim.chat_id,
        message_id=claim.message_id,
        versao_prompt=VERSAO_PROMPT,
    )
    return content, mime, reading


async def _complete(
    engine: AsyncEngine,
    claim: PhotoClaim,
    *,
    content: bytes | None = None,
    mime: str | None = None,
    reading: dict[str, object] | None = None,
    error_code: str | None = None,
    retryable: bool = False,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            await _set_current_user(session, claim.user_id)
            draft = await session.scalar(
                select(RascunhoAposta)
                .where(
                    RascunhoAposta.id == claim.id,
                    RascunhoAposta.usuario_id == claim.user_id,
                    RascunhoAposta.status.in_(ACTIVE_DRAFT_STATUSES),
                    RascunhoAposta.extraction_completed_at.is_(None),
                    RascunhoAposta.extraction_lease_token == claim.token,
                )
                .with_for_update()
            )
            if draft is None:
                return
            draft.extraction_lease_token = None
            draft.extraction_lease_until = None
            draft.extraction_error_code = error_code
            if retryable and claim.attempts < MAX_ATTEMPTS:
                draft.extraction_next_attempt_at = _now() + timedelta(
                    seconds=min(300, 2**claim.attempts)
                )
                return
            draft.extraction_completed_at = _now()
            media_hash = None
            if content is not None and mime is not None:
                media_hash = hashlib.sha256(content).hexdigest()
                await MidiaRepo().upsert_idempotent(session, media_hash, mime, len(content))
                await MidiaArquivoRepo().upsert_idempotent(session, media_hash, content)
                source = dict(draft.source_metadata_json)
                source["content_hash"] = media_hash
                draft.source_metadata_json = source
            candidates = candidates_from_reading(reading or {})
            if not candidates and isinstance(reading, dict) and reading.get("grave"):
                draft.extraction_error_code = "extraction_failed"
            if len(candidates) > 1:
                draft.coupon_candidates_json = candidates
                draft.status = "AWAITING_INFORMATION"
                draft.updated_at = _now()
                await session.flush()
            else:
                candidate = candidates[0] if candidates else {"fields": {}, "confidence": {}}
                await apply_extraction(
                    session,
                    draft,
                    candidate["fields"],
                    candidate["confidence"],
                    media_hash=media_hash,
                )
            if media_hash is not None:
                draft.media_hash = media_hash
            await queue_reply(
                session,
                user_id=claim.user_id,
                chat_id=claim.chat_id,
                key=f"telegram-photo:{claim.id}:extraction",
                message=await draft_summary(session, draft),
            )


async def process_photo_once(
    engine: AsyncEngine, client: TelegramClient, *, draft_id: UUID | None = None
) -> bool:
    claim = await claim_photo(engine, draft_id=draft_id)
    if claim is None:
        return False
    started = perf_counter()
    try:
        with observe_stage("telegram_photo"):
            content, mime, reading = await read_photo(claim, client)
        await _complete(engine, claim, content=content, mime=mime, reading=reading)
    except (TelegramApiError, InvalidTelegramUpdateError) as exc:
        reason = exc.reason if isinstance(exc, TelegramApiError) else "invalid_reference"
        retryable = isinstance(exc, TelegramApiError) and exc.retryable
        await _complete(engine, claim, error_code=reason, retryable=retryable)
        structlog.get_logger(__name__).warning("telegram_photo_retry", reason=reason)
    except RETRY_ON:
        await _complete(engine, claim, error_code="extraction_unavailable", retryable=True)
        structlog.get_logger(__name__).warning(
            "telegram_photo_retry", reason="extraction_unavailable"
        )
    finally:
        structlog.get_logger(__name__).info(
            "telegram_photo_processed", elapsed_seconds=round(perf_counter() - started, 3)
        )
    return True
