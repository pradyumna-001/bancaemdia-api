"""Resumable Telegram draft conversation; every caller owns the transaction."""

from dataclasses import dataclass
from math import isfinite
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.account_attribution import ResolutionStatus
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
)
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.domain.rascunho_aposta import (
    DraftInputError,
    missing_fields,
    occurrence,
    parse_reply,
    summary,
    valid_value,
)
from bancaemdia.integrations.telegram.codec import encrypt_payload
from bancaemdia.models.rascunho_aposta import RascunhoAposta
from bancaemdia.repositories.rascunho_aposta import RascunhoApostaRepo

REPO = RascunhoApostaRepo()
EXTRACTED_FIELDS = frozenset({
    "casa",
    "odd",
    "stake_unidades",
    "data_aposta",
    "evento",
    "descricao",
    "mercado_bruto",
    "tipo_aposta",
    "tipster",
    "freebet",
})


@dataclass(frozen=True)
class DraftReply:
    text: str
    draft: RascunhoAposta | None = None


def _normalized_fields(fields: dict[str, Any]) -> dict[str, Any]:
    safe = {key: value for key, value in fields.items() if key in EXTRACTED_FIELDS}
    if "casa" in safe and isinstance(safe["casa"], str):
        safe["casa"] = casa_canonica(safe["casa"]) or safe["casa"][:120]
    for key, value in list(safe.items()):
        if isinstance(value, float) and not isfinite(value):
            del safe[key]
            continue
        if hasattr(value, "isoformat"):
            safe[key] = value.isoformat()
        elif isinstance(value, str):
            safe[key] = value[:500]
        elif isinstance(value, bool):
            if key != "freebet":
                del safe[key]
        elif not isinstance(value, (int, float)):
            del safe[key]
    return safe


def _confidence(values: dict[str, float] | None, key: str) -> float:
    value = (values or {}).get(key, 1.0)
    if not isinstance(value, (int, float)) or not isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


async def _assess(
    session: AsyncSession,
    user_id: int,
    fields: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], str]:
    values = dict(fields)
    meta = dict(metadata)
    house = values.get("casa")
    instant = occurrence(values)
    account_needs_choice = False
    if valid_value("casa", house) and instant is not None:
        explicit = (
            values.get("conta_casa_id")
            if isinstance(meta.get("conta_casa_id"), dict)
            and meta["conta_casa_id"].get("source") == "user"
            else None
        )
        try:
            resolution = await attribute_account(
                session,
                user_id,
                str(house),
                instant,
                explicit if type(explicit) is int else None,
            )
        except InvalidAccountReferenceError as exc:
            raise DraftInputError(str(exc)) from exc
        if resolution.status == ResolutionStatus.UNIQUE:
            values["conta_casa_id"] = resolution.conta_casa_id
            if explicit is None:
                meta["conta_casa_id"] = {"source": "resolver", "confidence": 1.0}
        else:
            values.pop("conta_casa_id", None)
            meta.pop("conta_casa_id", None)
            account_needs_choice = True
    else:
        values.pop("conta_casa_id", None)
        meta.pop("conta_casa_id", None)
    missing = missing_fields(values, meta, account_needs_choice=account_needs_choice)
    status = "AWAITING_INFORMATION" if missing else "AWAITING_CONFIRMATION"
    return values, meta, missing, status


async def open_draft(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    update_id: int,
    media_file_id: str | None = None,
    media_hash: str | None = None,
    extracted: dict[str, Any] | None = None,
    confidence: dict[str, float] | None = None,
) -> tuple[RascunhoAposta, bool]:
    """Create once for a Telegram photo; a concurrent active draft wins."""
    existing = await REPO.active(session, user_id, chat_id, lock=True)
    if existing is not None:
        return existing, False
    fields = _normalized_fields(extracted or {})
    meta = {
        key: {"source": "extraction", "confidence": _confidence(confidence, key)} for key in fields
    }
    if extracted is None:
        missing: list[str] = []
        status = "AWAITING_EXTRACTION"
    else:
        fields, meta, missing, status = await _assess(session, user_id, fields, meta)
    ciphertext = encrypt_payload({"file_id": media_file_id}) if media_file_id else None
    return await REPO.create(
        session,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        update_id=update_id,
        media_reference_ciphertext=ciphertext,
        media_hash=media_hash,
        fields=fields,
        metadata=meta,
        missing=missing,
        status=status,
    )


async def apply_extraction(
    session: AsyncSession,
    draft: RascunhoAposta,
    extracted: dict[str, Any],
    confidence: dict[str, float] | None = None,
    *,
    media_hash: str | None = None,
) -> RascunhoAposta:
    """Future photo reader fills fields without replacing user corrections."""
    fields = dict(draft.fields_json)
    meta = dict(draft.field_meta_json)
    changes: dict[str, Any] = {}
    for key, value in _normalized_fields(extracted).items():
        current_meta = meta.get(key)
        if isinstance(current_meta, dict) and current_meta.get("source") == "user":
            continue
        if fields.get(key) != value:
            fields[key] = value
            changes[key] = value
        meta[key] = {"source": "extraction", "confidence": _confidence(confidence, key)}
    fields, meta, missing, status = await _assess(session, draft.usuario_id, fields, meta)
    saved = await REPO.save(
        session,
        draft,
        expected_version=draft.version,
        fields=fields,
        metadata=meta,
        missing=missing,
        status=status,
        changes=changes,
        update_id=None,
    )
    if media_hash is not None:
        saved.media_hash = media_hash
        await session.flush()
    return saved


async def new_photo_reply(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    update_id: int,
    media_file_id: str,
) -> DraftReply:
    draft, created = await open_draft(
        session,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        update_id=update_id,
        media_file_id=media_file_id,
    )
    if not created:
        return DraftReply(
            "Já existe um rascunho neste chat. Use /continuar para retomá-lo ou "
            "/cancelar antes de enviar outra foto.",
            draft,
        )
    return DraftReply(summary(draft.fields_json, draft.missing_fields_json, draft.status), draft)


async def handle_text(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    update_id: int,
    text: str,
) -> DraftReply | None:
    draft = await REPO.active(session, user_id, chat_id, lock=True)
    if draft is None:
        if text.strip().lower().startswith(("/continuar", "/corrigir", "/cancelar")):
            return DraftReply("Não há rascunho ativo. Envie uma foto de uma aposta para começar.")
        return None
    command = text.strip().lower()
    if command == "/continuar":
        return DraftReply(
            summary(draft.fields_json, draft.missing_fields_json, draft.status), draft
        )
    if command == "/cancelar":
        await REPO.close(session, draft, "CANCELLED")
        return DraftReply("Rascunho cancelado. Você pode enviar outra foto.", draft)
    try:
        patch = parse_reply(text)
        fields = dict(draft.fields_json)
        meta = dict(draft.field_meta_json)
        if ("casa" in patch or "data_aposta" in patch) and (
            meta.get("conta_casa_id") == {"source": "resolver", "confidence": 1.0}
        ):
            fields.pop("conta_casa_id", None)
            meta.pop("conta_casa_id", None)
        changes = {key: value for key, value in patch.items() if fields.get(key) != value}
        if not changes:
            return DraftReply(summary(fields, draft.missing_fields_json, draft.status), draft)
        fields.update(changes)
        for key in changes:
            meta[key] = {"source": "user", "confidence": 1.0}
        fields, meta, missing, status = await _assess(session, user_id, fields, meta)
    except DraftInputError as exc:
        return DraftReply(
            f"{exc}\n" + summary(draft.fields_json, draft.missing_fields_json, draft.status),
            draft,
        )
    saved = await REPO.save(
        session,
        draft,
        expected_version=draft.version,
        fields=fields,
        metadata=meta,
        missing=missing,
        status=status,
        changes=changes,
        update_id=update_id,
    )
    return DraftReply(summary(saved.fields_json, saved.missing_fields_json, saved.status), saved)
