"""Explicit, transaction-owned Telegram draft confirmation."""

from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.rascunho_aposta import DraftInputError
from bancaemdia.models.rascunho_aposta import ACTIVE_DRAFT_STATUSES, RascunhoAposta
from bancaemdia.observability.tracing import custom_span, set_custom_span_attributes
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.rascunho_aposta import RascunhoApostaRepo
from bancaemdia.services.telegram_conversation import _assess, draft_summary
from bancaemdia.workers.telegram import queue_reply
from bancaemdia.workers.telegram_materialization import (
    SavedDraftBet,
    materialize_draft,
    read_saved_bet,
)

REPO = RascunhoApostaRepo()


@dataclass(frozen=True)
class ConfirmationReply:
    text: str
    queued: bool = False


async def _latest_draft(session: AsyncSession, user_id: int, chat_id: int) -> RascunhoAposta | None:
    return cast(
        RascunhoAposta | None,
        await session.scalar(
            select(RascunhoAposta)
            .where(RascunhoAposta.usuario_id == user_id, RascunhoAposta.telegram_chat_id == chat_id)
            .order_by(RascunhoAposta.created_at.desc(), RascunhoAposta.telegram_message_id.desc())
            .with_for_update()
            .limit(1)
        ),
    )


async def saved_summary(session: AsyncSession, saved: SavedDraftBet) -> str:
    bet, state = saved.bet, saved.state
    lines = [f"Aposta registrada: #{bet.id}."]
    house = state.get("casa")
    if house:
        lines.append(f"Casa: {house}.")
    if bet.conta_casa_id is not None:
        account = await ContaCasaRepo().get_by_id(session, bet.usuario_id, bet.conta_casa_id)
        alias = f" ({account.apelido})" if account is not None and account.apelido else ""
        lines.append(f"Conta: {bet.conta_casa_id}{alias}.")
    for field, label in (
        ("evento", "Evento"),
        ("mercado_bruto", "Mercado"),
        ("descricao", "Seleção"),
    ):
        value = state.get(field)
        if value:
            lines.append(f"{label}: {value}.")
    if bet.odd is not None:
        lines.append(f"Odd: {bet.odd:g}.")
    lines.append(f"Stake: {bet.stake_unidades:g} unidades ({bet.stake_centavos} centavos).")
    if bet.data_aposta is not None:
        lines.append(f"Data da aposta: {bet.data_aposta.isoformat()}.")
    if bet.data_jogo is not None:
        lines.append(f"Data do jogo: {bet.data_jogo.isoformat()}.")
    if bet.revisao_grave or not bet.selecionada:
        lines.append("Esta aposta está em revisão no aplicativo.")
    return "\n".join(lines)


async def _queue_success(
    session: AsyncSession, draft: RascunhoAposta, saved: SavedDraftBet
) -> ConfirmationReply:
    key = f"telegram-confirmation:{draft.id}:success"
    message = await saved_summary(session, saved)
    await queue_reply(
        session,
        user_id=draft.usuario_id,
        chat_id=draft.telegram_chat_id,
        key=key,
        message=message,
    )
    return ConfirmationReply(message, queued=True)


async def confirm_draft(
    session: AsyncSession, *, user_id: int, chat_id: int, update_id: int
) -> ConfirmationReply:
    """Caller must own the inbox transaction and have set the linked tenant GUC."""
    draft = await _latest_draft(session, user_id, chat_id)
    if draft is None or draft.status in {"CANCELLED", "FAILED"}:
        return ConfirmationReply("Não há rascunho para confirmar. Envie uma foto para começar.")
    with custom_span("telegram.confirmation", update_id=update_id, draft_id=str(draft.id)) as span:
        saved = await read_saved_bet(session, user_id=user_id, draft_id=draft.id)
        if saved is not None:
            if draft.status in ACTIVE_DRAFT_STATUSES:
                await REPO.close(session, draft, "CONFIRMED")
            reply = await _queue_success(session, draft, saved)
            set_custom_span_attributes(
                span,
                "telegram.confirmation",
                event_id=saved.event_id,
                bet_id=saved.bet.id,
                outbox_key=f"telegram-confirmation:{draft.id}:success",
            )
            return reply
        if draft.status == "CONFIRMED":
            raise RuntimeError("confirmed draft has no bet")
        if draft.coupon_candidates_json:
            return ConfirmationReply(await draft_summary(session, draft))
        if draft.extraction_completed_at is None:
            return ConfirmationReply(
                "A leitura da foto ainda está pendente. Use /continuar em instantes."
            )
        try:
            fields, metadata, missing, status = await _assess(
                session, user_id, draft.fields_json, draft.field_meta_json
            )
        except DraftInputError as exc:
            return ConfirmationReply(f"{exc}\n" + await draft_summary(session, draft))
        changed = (
            fields != draft.fields_json
            or metadata != draft.field_meta_json
            or missing != draft.missing_fields_json
            or status != draft.status
        )
        if changed:
            draft = await REPO.save(
                session,
                draft,
                expected_version=draft.version,
                fields=fields,
                metadata=metadata,
                missing=missing,
                status=status,
                changes={},
                update_id=update_id,
            )
            return ConfirmationReply(
                "Os dados mudaram desde o último resumo. Confira e envie /confirmar novamente.\n"
                + await draft_summary(session, draft)
            )
        if missing or status != "AWAITING_CONFIRMATION":
            return ConfirmationReply(await draft_summary(session, draft))
        saved = await materialize_draft(session, draft, fields, confirmation_update_id=update_id)
        await REPO.close(session, draft, "CONFIRMED")
        reply = await _queue_success(session, draft, saved)
        set_custom_span_attributes(
            span,
            "telegram.confirmation",
            event_id=saved.event_id,
            bet_id=saved.bet.id,
            outbox_key=f"telegram-confirmation:{draft.id}:success",
        )
        return reply
