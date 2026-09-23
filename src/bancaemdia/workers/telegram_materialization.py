"""Adapt a confirmed Telegram draft to the existing event-first materializer."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.materializar import NovaAposta
from bancaemdia.domain.projecao import projetar
from bancaemdia.domain.rascunho_aposta import occurrence
from bancaemdia.domain.registros import Aposta
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.models.rascunho_aposta import RascunhoAposta
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo
from bancaemdia.workers.materialization import _gravar


class ConfirmationMaterializationError(RuntimeError):
    pass


def draft_bet_key(draft_id: UUID) -> str:
    return f"telegram_draft:{draft_id}"


@dataclass(frozen=True)
class SavedDraftBet:
    bet: Aposta
    state: dict[str, Any]
    event_id: int


async def read_saved_bet(
    session: AsyncSession, *, user_id: int, draft_id: UUID
) -> SavedDraftBet | None:
    key = draft_bet_key(draft_id)
    bet = await ApostaRepo().get_by_chave(session, user_id, key)
    if bet is None:
        return None
    events = await EventoRepo().list_by_aposta_chave(session, user_id, key)
    if not events:
        raise ConfirmationMaterializationError("bet without source event")
    state, _ = projetar((event.tipo, event.fonte, event.payload_json) for event in events)
    return SavedDraftBet(bet, state, events[0].id)


async def materialize_draft(
    session: AsyncSession,
    draft: RascunhoAposta,
    fields: dict[str, Any],
    *,
    confirmation_update_id: int,
) -> SavedDraftBet:
    existing = await read_saved_bet(session, user_id=draft.usuario_id, draft_id=draft.id)
    if existing is not None:
        return existing
    instant = occurrence(fields)
    account_id = fields.get("conta_casa_id")
    house = fields.get("casa")
    if instant is None or type(account_id) is not int or not isinstance(house, str):
        raise ConfirmationMaterializationError("draft lacks a resolved account or date")
    unit = await UnidadeRepo().get_vigente(session, draft.usuario_id, instant)
    unit_cents = VALOR_UNIDADE_PADRAO_CENTAVOS if unit is None else unit.valor_centavos
    account_meta = draft.field_meta_json.get("conta_casa_id")
    explicit = isinstance(account_meta, dict) and account_meta.get("source") == "user"
    payload: dict[str, Any] = {
        "origem": "telegram_bot",
        "telegram_draft_id": str(draft.id),
        "telegram_update_id": draft.telegram_update_id,
        "telegram_confirmation_update_id": confirmation_update_id,
        "telegram_chat_id": draft.telegram_chat_id,
        "telegram_message_id": draft.telegram_message_id,
        "casa": house,
        "conta_casa_id": account_id,
        "conta_referencia_explicita": explicit,
        "evento": fields.get("evento"),
        "descricao": fields.get("descricao"),
        "mercado_bruto": fields.get("mercado_bruto"),
        "tipo_aposta": fields.get("tipo_aposta") or "SIMPLES",
        "odd": fields["odd"],
        "stake_unidades": fields["stake_unidades"],
        "data_aposta": instant.isoformat(),
        "valor_unidade_centavos": unit_cents,
        "freebet": fields.get("freebet") is True,
        "selecionada": True,
        "revisao_grave": False,
    }
    for field in (
        "data_jogo",
        "tipster_id",
        "time_casa_id",
        "time_fora_id",
        "mercado_id",
        "competicao_id",
    ):
        if fields.get(field) is not None:
            payload[field] = fields[field]
    # The bet row's Telegram message columns stay NULL: the export pipeline owns positive
    # chat/message coordinates and has its own uniqueness constraint.
    nova = NovaAposta(
        chave=draft_bet_key(draft.id),
        ordem=0,
        origem="telegram_bot",
        chat_id=None,
        message_id=None,
        bilhete=None,
        casa=house,
        data_aposta=instant.isoformat(),
        revisao_motivo=None,
        revisao_grave=False,
        confianca=1.0,
        payload=payload,
    )
    await _gravar(session, draft.usuario_id, nova, {}, draft.media_hash)
    saved = await read_saved_bet(session, user_id=draft.usuario_id, draft_id=draft.id)
    if saved is None or saved.bet.conta_casa_id != account_id:
        raise ConfirmationMaterializationError("account changed while materializing draft")
    return saved
