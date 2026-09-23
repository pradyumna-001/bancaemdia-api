"""Resumable Telegram draft conversation; every caller owns the transaction."""

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any, cast

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
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.rascunho_aposta import RascunhoApostaRepo

REPO = RascunhoApostaRepo()
EXTRACTED_FIELDS = frozenset({
    "casa",
    "odd",
    "stake_unidades",
    "data_aposta",
    "data_jogo",
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


async def _draft_summary(session: AsyncSession, draft: RascunhoAposta) -> str:
    message = summary(draft.fields_json, draft.missing_fields_json, draft.status)
    if "conta_casa_id" not in draft.missing_fields_json:
        account_id = draft.fields_json.get("conta_casa_id")
        if type(account_id) is int:
            account = await ContaCasaRepo().get_by_id(session, draft.usuario_id, account_id)
            if account is not None:
                label = account.apelido or "sem apelido"
                message = message.replace(
                    f"• conta da casa: {account_id}",
                    f"• conta da casa: {account_id} ({label})",
                )
        return message
    house = draft.fields_json.get("casa")
    house_id = await CasaRepo().get_id_by_nome(session, str(house)) if house else None
    accounts = (
        await ContaCasaRepo().list_by_house(session, draft.usuario_id, house_id)
        if house_id is not None
        else []
    )
    if not accounts:
        return message + "\nNão encontrei uma conta dessa casa; cadastre uma no aplicativo."
    choices = ", ".join(f"{item.id} ({item.apelido or 'sem apelido'})" for item in accounts)
    return message + f"\nContas desta casa: {choices}. Responda conta=<número>."


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
    media_reference: dict[str, Any] | None = None,
    source_metadata: dict[str, Any] | None = None,
    media_hash: str | None = None,
    extracted: dict[str, Any] | None = None,
    confidence: dict[str, float] | None = None,
) -> tuple[RascunhoAposta, bool]:
    """Create once for a Telegram photo; a concurrent active draft wins."""
    by_origin = await REPO.by_origin(session, user_id, chat_id, message_id)
    if by_origin is not None:
        return by_origin, False
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
    reference = media_reference or ({"file_id": media_file_id} if media_file_id else None)
    ciphertext = encrypt_payload(reference) if reference else None
    draft, created = await REPO.create(
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
    if created and source_metadata:
        draft.source_metadata_json = source_metadata
        await session.flush()
    if created and extracted is not None:
        draft.extraction_completed_at = datetime.now(UTC)
        await session.flush()
    return draft, created


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
    media_reference: dict[str, Any] | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> DraftReply:
    draft, created = await open_draft(
        session,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        update_id=update_id,
        media_file_id=media_file_id,
        media_reference=media_reference,
        source_metadata=source_metadata,
    )
    if not created:
        return DraftReply(
            "Já existe um rascunho neste chat. Use /continuar para retomá-lo ou "
            "/cancelar antes de enviar outra foto.",
            draft,
        )
    return DraftReply(await _draft_summary(session, draft), draft)


async def draft_summary(session: AsyncSession, draft: RascunhoAposta) -> str:
    if draft.coupon_candidates_json:
        return (
            f"Rascunho v{draft.version}. A foto parece conter {len(draft.coupon_candidates_json)} apostas. "
            "Qual única aposta você quer registrar? Responda cupom=1, cupom=2, etc. "
            "A foto já está guardada. Use /cancelar para desistir."
        )
    message = await _draft_summary(session, draft)
    if draft.extraction_completed_at is None and draft.status == "AWAITING_CONFIRMATION":
        message = message.replace(
            "Use /confirmar para registrar, ",
            "A leitura da foto ainda está pendente. Use ",
        )
    return message + f"\nVersão do rascunho: {draft.version}."


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
        if text.strip().lower().startswith(("/continuar", "/corrigir", "/cancelar", "/confirmar")):
            return DraftReply("Não há rascunho ativo. Envie uma foto de uma aposta para começar.")
        return None
    command = text.strip().lower()
    if command == "/continuar":
        return DraftReply(await draft_summary(session, draft), draft)
    if command == "/cancelar":
        await REPO.close(session, draft, "CANCELLED")
        return DraftReply("Rascunho cancelado. Você pode enviar outra foto.", draft)
    if draft.coupon_candidates_json:
        if command.startswith("cupom="):
            try:
                chosen = int(command.removeprefix("cupom=").strip())
            except ValueError:
                chosen = 0
            if 1 <= chosen <= len(draft.coupon_candidates_json):
                candidate = draft.coupon_candidates_json[chosen - 1]
                fields = candidate.get("fields")
                confidence = candidate.get("confidence")
                if not isinstance(fields, dict) or not isinstance(confidence, dict):
                    return DraftReply("Não consegui abrir esse cupom. Use /cancelar.", draft)
                draft.coupon_candidates_json = []
                saved = await apply_extraction(
                    session,
                    draft,
                    cast(dict[str, Any], fields),
                    cast(dict[str, float], confidence),
                )
                return DraftReply(await draft_summary(session, saved), saved)
        return DraftReply(
            "Escolha uma única aposta com cupom=1, cupom=2, etc.\n"
            + await draft_summary(session, draft),
            draft,
        )
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
            return DraftReply(await draft_summary(session, draft), draft)
        fields.update(changes)
        for key in changes:
            meta[key] = {"source": "user", "confidence": 1.0}
        fields, meta, missing, status = await _assess(session, user_id, fields, meta)
    except DraftInputError as exc:
        return DraftReply(
            f"{exc}\n" + await draft_summary(session, draft),
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
    return DraftReply(await draft_summary(session, saved), saved)
