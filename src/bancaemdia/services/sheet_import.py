"""Synchronous bounded previews and atomic, serializable financial application."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.v1.apostas import _aplicar, _data_do_estado, _historico, _travar
from bancaemdia.config import get_settings
from bancaemdia.domain.account_attribution import review_reason
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
    explicit_reference,
)
from bancaemdia.domain.account_review import sync_account_review
from bancaemdia.domain.financeiro import Aposta as Financeira
from bancaemdia.domain.materializar import EventoNovo, projetar
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.sheet_xlsx import (
    SheetError,
    default_mapping,
    digest,
    normalized_row,
    validate_mapping,
)
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.models.sheet_import import SheetConfirmation, SheetPreview, SheetSource
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo

PREVIEW_DAYS = 1
RESULT_DAYS = 90


def now() -> datetime:
    return datetime.now(UTC)


def cipher() -> Fernet:
    secret = get_settings().COLETA_TOKEN_SECRET
    return Fernet(
        base64.urlsafe_b64encode(hashlib.sha256(("sheet-import-v1:" + secret).encode()).digest())
    )


def seal(value: object) -> bytes:
    return cipher().encrypt(json.dumps(value, ensure_ascii=False, default=str).encode())


def unseal(value: bytes | None) -> dict[str, Any]:
    if value is None:
        raise SheetError("import_expired", 410)
    try:
        result: dict[str, Any] = json.loads(cipher().decrypt(value))
        return result
    except (InvalidToken, ValueError):
        raise SheetError("import_unavailable", 503) from None


def origin(source: SheetSource) -> str:
    return f"web-xlsx:{source.id}"


def source_view(source: SheetSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "origem_id": origin(source),
        "nome": source.nome,
        "version": source.version,
        "current_preview": source.current_preview,
    }


async def lock(session: AsyncSession, uid: int) -> None:
    from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

    await CruzamentoCandidatoRepo().lock(session, uid)


async def source_get(session: AsyncSession, uid: int, identity: UUID) -> SheetSource:
    result = await session.get(SheetSource, (uid, identity))
    if result is None:
        raise SheetError("import_not_found", 404)
    return result


async def fingerprint(session: AsyncSession, uid: int) -> str:
    """Primary serializable snapshot of all facts/temporal inputs used by the plan."""
    snapshot = []
    for model in (
        models.Aposta,
        models.Evento,
        models.Unidade,
        models.ContaCasa,
        models.UsoContaCasa,
        models.RevisaoPendente,
        models.ApostaConsolidacao,
        models.Movimento,
    ):
        table = model.__table__
        rows = (
            (
                await session.execute(
                    select(table).where(table.c.usuario_id == uid).order_by(*table.primary_key)
                )
            )
            .mappings()
            .all()
        )
        snapshot.append([model.__tablename__, [dict(row) for row in rows]])
    return digest(snapshot)


async def plan(
    session: AsyncSession,
    uid: int,
    source: SheetSource,
    workbook: dict[str, Any],
    sheet_name: str,
    mapping: dict[str, int],
) -> dict[str, Any]:
    sheet = next((sheet for sheet in workbook["sheets"] if sheet["name"] == sheet_name), None)
    if sheet is None:
        raise SheetError("sheet_not_found")
    mapping_errors = validate_mapping(mapping, len(sheet["headers"]))
    rows = []
    ids: set[str] = set()
    counts = dict.fromkeys(("criadas", "atualizadas", "ignoradas", "recusadas", "revisoes"), 0)
    for raw in sheet["rows"]:
        normalized, errors = (
            normalized_row(raw, mapping) if not mapping_errors else ({}, list(mapping_errors))
        )
        identity = normalized.get("linha_id")
        if identity in ids:
            errors.append({"field": "linha_id", "code": "duplicate_row_id"})
        if identity is not None:
            ids.add(identity)
        row: dict[str, Any] = {
            "sheet": sheet_name,
            "number": raw["number"],
            "linha_id": identity,
            "raw": raw["values"],
            "normalized": normalized,
            "errors": errors,
            "consequence": "refuse",
            "bet_key": None,
            "review_reason": None,
            "stake_centavos": None,
            "valor_aposta_centavos": None,
            "conta_casa_id": None,
        }
        if errors:
            counts["recusadas"] += 1
            rows.append(row)
            continue
        key = "s:" + hashlib.sha256(f"{origin(source)}:id:{identity}".encode()).hexdigest()
        history = await _historico(session, uid, key)
        before, _ = projetar(history)
        previous = _data_do_estado(before.get("fonte_atualizada_em"))
        incoming = datetime.fromisoformat(normalized["atualizada_em"])
        row["bet_key"] = key
        if previous is not None and incoming <= previous:
            row["consequence"] = "ignore"
            counts["ignoradas"] += 1
            rows.append(row)
            continue
        bet_date = datetime.fromisoformat(normalized["data_aposta"])
        game_date = _data_do_estado(normalized.get("data_jogo"))
        unit = await UnidadeRepo().get_vigente(session, uid, bet_date)
        unit_value = VALOR_UNIDADE_PADRAO_CENTAVOS if unit is None else unit.valor_centavos
        reference = normalized.get("conta_casa_ref")
        reference = reference if reference is not None else explicit_reference(before)
        try:
            account = await attribute_account(
                session, uid, normalized["casa"], game_date, reference
            )
        except InvalidAccountReferenceError:
            row["errors"].append({"field": "conta_casa_ref", "code": "invalid_account_reference"})
            counts["recusadas"] += 1
            rows.append(row)
            continue
        payload = {
            "origem": "planilha",
            "linha_hash": key[2:],
            "sheet_source": origin(source),
            "sheet_row_id": identity,
            "casa": normalized["casa"],
            "data_aposta": normalized["data_aposta"],
            "data_jogo": normalized.get("data_jogo"),
            "fonte_atualizada_em": normalized["atualizada_em"],
            "odd": normalized["odd"],
            "stake_unidades": normalized["stake_unidades"],
            "valor_unidade_centavos": unit_value,
            "conta_casa_id": account.conta_casa_id,
            "conta_referencia_explicita": reference is not None,
            "conta_casa_ref": reference,
        }
        payload.update({
            field: normalized[field]
            for field in ("evento", "descricao", "mercado_bruto", "freebet")
            if field in normalized
        })
        if not history:
            payload["selecionada"] = True
            payload["snapshot_replay"] = {
                "snapshot_completo": True,
                **{
                    field: payload.get(field)
                    for field in (
                        "origem",
                        "stake_unidades",
                        "valor_unidade_centavos",
                        "conta_casa_id",
                        "data_aposta",
                        "data_jogo",
                        "freebet",
                    )
                },
                "chat_id": None,
                "message_id": None,
                "ordem_na_mensagem": 0,
            }
            event = EventoNovo("APOSTA_CRIADA", "planilha", payload)
            row["consequence"] = "create"
            counts["criadas"] += 1
        else:
            event = EventoNovo(
                "CORRECAO_MANUAL",
                "planilha",
                {
                    field: value
                    for field, value in payload.items()
                    if field != "origem" and before.get(field) != value
                },
            )
            row["consequence"] = "update"
            counts["atualizadas"] += 1
        after, _ = projetar([*history, (event.tipo, event.fonte, event.payload)])
        finance = Financeira(
            stake_unidades=float(after["stake_unidades"]),
            valor_unidade_centavos=int(after["valor_unidade_centavos"]),
            freebet=bool(after.get("freebet")),
        )
        monetary_value = finance.stake_unidades * finance.valor_unidade_centavos
        if not math.isfinite(monetary_value) or monetary_value >= 2**63:
            row["errors"].append({
                "field": "stake_unidades",
                "code": "financial_value_out_of_range",
            })
            counts["criadas" if row["consequence"] == "create" else "atualizadas"] -= 1
            counts["recusadas"] += 1
            row["consequence"] = "refuse"
            rows.append(row)
            continue
        row.update(
            stake_centavos=finance.stake_centavos,
            valor_aposta_centavos=finance.valor_aposta_centavos,
            conta_casa_id=account.conta_casa_id,
            review_reason=review_reason(account),
            internal_event={"tipo": event.tipo, "fonte": event.fonte, "payload": event.payload},
            internal_after=after,
        )
        if row["review_reason"]:
            counts["revisoes"] += 1
        rows.append(row)
    return {
        "sheet": sheet_name,
        "mapping": mapping,
        "mapping_errors": mapping_errors,
        "rows": rows,
        "counts": counts,
        "snapshot": await fingerprint(session, uid),
    }


async def preview_get(session: AsyncSession, uid: int, identity: UUID) -> SheetPreview:
    preview = await session.get(SheetPreview, (uid, identity))
    if preview is None:
        raise SheetError("import_not_found", 404)
    if preview.expires_at <= now():
        raise SheetError("import_expired", 410)
    return preview


async def preview_view(session: AsyncSession, preview: SheetPreview) -> dict[str, Any]:
    private = unseal(preview.private_data)
    source = await source_get(session, preview.usuario_id, preview.source_id)
    plan_data = private["plan"]
    state = (
        "obsolete"
        if source.current_preview != preview.id
        or source.version != preview.source_version
        or plan_data["snapshot"] != await fingerprint(session, preview.usuario_id)
        else "invalid"
        if plan_data["counts"]["recusadas"] or plan_data["mapping_errors"]
        else "ready"
    )
    return {
        "id": preview.id,
        "source_id": source.id,
        "origem_id": origin(source),
        "revision": preview.revision,
        "source_version": preview.source_version,
        "preview_hash": preview.preview_hash,
        "file_hash": preview.file_hash,
        "state": state,
        "sheets": [
            {"name": sheet["name"], "headers": sheet["headers"], "total_rows": len(sheet["rows"])}
            for sheet in private["workbook"]["sheets"]
        ],
        "sheet": plan_data["sheet"],
        "mapping": plan_data["mapping"],
        "mapping_errors": plan_data["mapping_errors"],
        "total_rows": len(plan_data["rows"]),
        "counts": plan_data["counts"],
        "expires_at": preview.expires_at,
    }


async def save_preview(
    session: AsyncSession,
    uid: int,
    source: SheetSource,
    identity: UUID,
    request_hash: str,
    file_hash: str,
    workbook: dict[str, Any],
    sheet: str | None = None,
    mapping: dict[str, int] | None = None,
    expiry: datetime | None = None,
) -> SheetPreview:
    existing = await session.get(SheetPreview, (uid, identity))
    if existing is not None:
        if existing.request_hash != request_hash or existing.source_id != source.id:
            raise SheetError("identity_conflict", 409)
        return await preview_get(session, uid, identity)
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.count())
        .select_from(SheetPreview)
        .where(SheetPreview.usuario_id == uid, SheetPreview.created_at >= today)
    )
    if int(total or 0) >= get_settings().SHEET_DAILY_PREVIEW_LIMIT:
        raise SheetError("preview_quota", 429)
    selected = sheet or workbook["sheets"][0]["name"]
    selected_sheet = next((item for item in workbook["sheets"] if item["name"] == selected), None)
    if selected_sheet is None:
        raise SheetError("sheet_not_found")
    resolved_mapping = default_mapping(selected_sheet) if mapping is None else mapping
    prepared = await plan(session, uid, source, workbook, selected, resolved_mapping)
    previous = (
        await session.get(SheetPreview, (uid, source.current_preview))
        if source.current_preview is not None
        else None
    )
    revision = 1 if previous is None else previous.revision + 1
    expiry = expiry or now() + timedelta(days=PREVIEW_DAYS)
    preview_hash = digest({
        "identity": identity,
        "source": source.id,
        "version": source.version,
        "file": file_hash,
        "plan": prepared,
    })
    record = SheetPreview(
        usuario_id=uid,
        id=identity,
        source_id=source.id,
        revision=revision,
        source_version=source.version,
        request_hash=request_hash,
        preview_hash=preview_hash,
        file_hash=file_hash,
        private_data=seal({"workbook": workbook, "plan": prepared}),
        expires_at=expiry,
    )
    session.add(record)
    source.current_preview = identity
    await session.flush()
    return record


async def confirm(
    session: AsyncSession, usuario: Usuario, identity: UUID, preview_id: UUID, preview_hash: str
) -> dict[str, Any]:
    uid = usuario.id
    await lock(session, uid)
    request_hash = digest([preview_id, preview_hash])
    existing = await session.get(SheetConfirmation, (uid, identity))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise SheetError("identity_conflict", 409)
        if existing.expires_at <= now():
            raise SheetError("import_expired", 410)
        return unseal(existing.private_result)
    preview = await preview_get(session, uid, preview_id)
    source = await source_get(session, uid, preview.source_id)
    if (
        preview.preview_hash != preview_hash
        or source.current_preview != preview.id
        or source.version != preview.source_version
    ):
        raise SheetError("preview_obsolete", 409)
    private = unseal(preview.private_data)
    old_plan = private["plan"]
    if old_plan["counts"]["recusadas"] or old_plan["mapping_errors"]:
        raise SheetError("preview_has_errors", 422)
    for row in sorted(old_plan["rows"], key=lambda row: row["bet_key"]):
        if not await _travar(session, uid, row["bet_key"]):
            raise SheetError("preview_obsolete", 409)
    current = await plan(
        session, uid, source, private["workbook"], old_plan["sheet"], old_plan["mapping"]
    )
    if digest(current) != digest(old_plan):
        raise SheetError("preview_obsolete", 409)
    from bancaemdia.domain.access import require_write_access

    await require_write_access(session, uid)
    applied = []
    for row in current["rows"]:
        key = row["bet_key"]
        if row["consequence"] != "ignore":
            event_data = row["internal_event"]
            event = EventoNovo(event_data["tipo"], event_data["fonte"], event_data["payload"])
            bet = await _aplicar(session, usuario, key, [event], row["internal_after"])
            if bet is None:
                raise SheetError("preview_obsolete", 409)
            normalized = row["normalized"]
            reference = row["internal_event"]["payload"].get("conta_casa_ref")
            account = await attribute_account(
                session,
                uid,
                normalized["casa"],
                _data_do_estado(normalized.get("data_jogo")),
                reference,
            )
            await sync_account_review(
                session,
                uid,
                key,
                account,
                context={
                    "sheet_source": origin(source),
                    "sheet_row_id": row["linha_id"],
                    "aba": row["sheet"],
                    "linha": row["number"],
                },
            )
        reviews = await RevisaoPendenteRepo().list_abertas_by_aposta_chave(session, uid, key)
        applied.append({
            "linha_id": row["linha_id"],
            "sheet": row["sheet"],
            "number": row["number"],
            "outcome": {"create": "created", "update": "updated", "ignore": "ignored"}[
                row["consequence"]
            ],
            "bet_key": key,
            "review_ids": [review.id for review in reviews],
        })
    source.version += 1
    expiry = now() + timedelta(days=RESULT_DAYS)
    result = {
        "id": str(identity),
        "preview_id": str(preview_id),
        "source_id": str(source.id),
        "source_version": source.version,
        "state": "completed",
        "atomic": True,
        "counts": current["counts"],
        "rows": applied,
        "expires_at": expiry.isoformat(),
        "aggregate_freshness": "materialized_views_eventual",
    }
    session.add(
        SheetConfirmation(
            usuario_id=uid,
            id=identity,
            preview_id=preview.id,
            source_id=source.id,
            request_hash=request_hash,
            private_result=seal(result),
            expires_at=expiry,
        )
    )
    await session.flush()
    return result
