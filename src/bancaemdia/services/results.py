"""Keyset queue and locked result/undo with durable private receipts."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi.encoders import jsonable_encoder
from sqlalchemy import String, func, literal, literal_column, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.v1.apostas import _aplicar, _historico, _travar
from bancaemdia.config import get_settings
from bancaemdia.domain.aposta_service import (
    ApostaInvalidaError,
    eventos_do_resultado,
    validar_resultado,
)
from bancaemdia.domain.materializar import EventoNovo, projetar
from bancaemdia.domain.registros import Aposta, Usuario
from bancaemdia.domain.result_contract import QueueFilter, ResultInput, UndoInput
from bancaemdia.models.result_operation import ResultOperation
from bancaemdia.repositories.aposta_consolidacao import financial_predicate
from bancaemdia.repositories.aposta_contexto_repo import ApostaContextoRepo
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.base import colunas
from bancaemdia.repositories.evento_repo import EventoRepo

FINANCIAL_FIELDS = ("estado", "retorno_centavos", "retorno_informado", "comissao_centavos")
UNDO_HOURS = 24
READ_DAYS = 90


class ResultError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code, self.status = code, status


def now() -> datetime:
    return datetime.now(UTC)


def digest(value: object) -> str:
    return hmac.new(
        ("results-v1:" + get_settings().COLETA_TOKEN_SECRET).encode(),
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode(),
        hashlib.sha256,
    ).hexdigest()


def cipher() -> Fernet:
    return Fernet(
        base64.urlsafe_b64encode(
            hashlib.sha256(
                ("result-cursor-v1:" + get_settings().COLETA_TOKEN_SECRET).encode()
            ).digest()
        )
    )


async def revision(session: AsyncSession, uid: int, key: str) -> str:
    value = await session.scalar(
        text("SELECT result_revision FROM apostas WHERE usuario_id=:uid AND chave=:key"),
        {"uid": uid, "key": key},
    )
    if value is None:
        raise ResultError("result_not_found", 404)
    return str(value)


async def blocked(
    session: AsyncSession, bet: Aposta, *, financial_eligible: bool | None = None
) -> str | None:
    if not bet.selecionada:
        return "deleted"
    if bet.revisao_grave:
        return "severe_review"
    if financial_eligible is None:
        financial_eligible = bool(
            await session.scalar(
                select(financial_predicate())
                .select_from(models.Aposta)
                .where(models.Aposta.usuario_id == bet.usuario_id, models.Aposta.id == bet.id)
            )
        )
    if not financial_eligible:
        return "contextual_source"
    if bet.conta_casa_id is None:
        return "account_unassigned"
    return None


async def view(
    session: AsyncSession,
    bet: Aposta,
    state: dict[str, Any],
    context: dict[str, Any] | None = None,
    queue_revision: str | None = None,
    *,
    known_revision: str | None = None,
    financial_eligible: bool | None = None,
) -> dict[str, Any]:
    if context is None:
        context = (await ApostaContextoRepo().list_by_ids(session, bet.usuario_id, [bet.id])).get(
            bet.id, {}
        )
    reason = (
        None
        if queue_revision is not None
        else await blocked(session, bet, financial_eligible=financial_eligible)
    )
    if reason is None and bet.estado != "PENDENTE":
        reason = "already_settled"
    return cast(
        dict[str, Any],
        jsonable_encoder({
            "chave": bet.chave,
            "revisao": known_revision
            or queue_revision
            or await revision(session, bet.usuario_id, bet.chave or ""),
            "estado": bet.estado,
            "elegivel": reason is None,
            "motivo_inelegivel": reason,
            "antiguidade": bet.data_jogo or bet.criada_em,
            "criterio_antiguidade": "data_jogo" if bet.data_jogo is not None else "criada_em",
            "origem": bet.origem,
            "casa": state.get("casa"),
            "evento": state.get("evento"),
            "descricao": state.get("descricao"),
            "mercado": state.get("mercado_bruto"),
            "data_jogo": bet.data_jogo,
            "data_aposta": bet.data_aposta,
            "odd": bet.odd,
            "stake_unidades": bet.stake_unidades,
            "stake_centavos": bet.stake_centavos,
            "valor_aposta_centavos": bet.valor_aposta_centavos,
            "retorno_centavos": bet.retorno_centavos,
            "retorno_informado": state.get("retorno_informado"),
            "comissao_centavos": state.get("comissao_centavos"),
            "lucro_centavos": None
            if bet.retorno_centavos is None
            else bet.retorno_centavos - bet.stake_centavos,
            "freebet": bet.freebet,
            "conta_atribuicao": "UNASSIGNED" if bet.conta_casa_id is None else "ASSIGNED",
            "conta_contexto": context.get("conta_contexto"),
            "banca_contexto": context.get("banca_contexto"),
            "tipster_id": bet.tipster_id,
            "mercado_id": bet.mercado_id,
            "competicao_id": bet.competicao_id,
        }),
    )


async def detail(session: AsyncSession, uid: int, key: str) -> dict[str, Any]:
    row = (
        await session.execute(
            select(
                models.Aposta,
                literal_column("result_revision", type_=String()),
                financial_predicate(),
            ).where(models.Aposta.usuario_id == uid, models.Aposta.chave == key)
        )
    ).one_or_none()
    if row is None:
        raise ResultError("result_not_found", 404)
    bet = Aposta(**colunas(row[0]))
    state, _ = projetar(await _historico(session, uid, key))
    return await view(
        session, bet, state, known_revision=str(row[1]), financial_eligible=bool(row[2])
    )


async def queue(session: AsyncSession, uid: int, query: QueueFilter) -> dict[str, Any]:
    filters = query.model_dump(mode="json", exclude={"cursor", "page_size"})
    instant = now()
    if query.cursor is not None:
        try:
            frontier = json.loads(cipher().decrypt(query.cursor.encode(), ttl=1800))
            if frontier["uid"] != uid or frontier["filters"] != digest(filters):
                raise ValueError("binding")
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise ResultError("result_cursor_invalid", 422) from None
    else:
        frontier = {
            "uid": uid,
            "filters": digest(filters),
            "max_id": int(
                await session.scalar(
                    select(func.coalesce(func.max(models.Aposta.id), 0)).where(
                        models.Aposta.usuario_id == uid
                    )
                )
                or 0
            ),
            "started": instant.isoformat(),
            "last": None,
        }
    bet = models.Aposta
    oldest = func.coalesce(bet.data_jogo, bet.criada_em)
    stmt = select(bet).where(
        bet.usuario_id == uid,
        bet.id <= frontier["max_id"],
        bet.chave.is_not(None),
        bet.estado == "PENDENTE",
        bet.selecionada,
        ~bet.revisao_grave,
        bet.conta_casa_id.is_not(None),
        financial_predicate(),
    )
    for field in (
        "conta_casa_id",
        "banca_id",
        "tipster_id",
        "mercado_id",
        "competicao_id",
        "time_casa_id",
        "time_fora_id",
        "origem",
        "freebet",
    ):
        value = getattr(query, field)
        if value is not None:
            stmt = stmt.where(getattr(bet, field) == value)
    if query.casa_id is not None or query.titular_id is not None:
        accounts = select(models.ContaCasa.id).where(models.ContaCasa.usuario_id == uid)
        if query.casa_id is not None:
            accounts = accounts.where(models.ContaCasa.casa_id == query.casa_id)
        if query.titular_id is not None:
            accounts = accounts.where(models.ContaCasa.titular_id == query.titular_id)
        stmt = stmt.where(bet.conta_casa_id.in_(accounts))
    if query.desde is not None:
        stmt = stmt.where(oldest >= query.desde)
    if query.ate is not None:
        stmt = stmt.where(oldest < query.ate)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    if frontier["last"] is not None:
        stamp, ident = frontier["last"]
        stmt = stmt.where(
            tuple_(oldest, bet.id) > tuple_(literal(datetime.fromisoformat(stamp)), literal(ident))
        )
    records = [
        Aposta(**colunas(row))
        for row in await session.scalars(stmt.order_by(oldest, bet.id).limit(query.page_size + 1))
    ]
    more = len(records) > query.page_size
    records = records[: query.page_size]
    histories = await EventoRepo().list_by_aposta_chaves(
        session, uid, [a.chave for a in records if a.chave is not None]
    )
    contexts = await ApostaContextoRepo().list_by_ids(session, uid, [a.id for a in records])
    revisions: dict[int, str] = {
        row[0]: str(row[1])
        for row in (
            await session.execute(
                select(bet.id, literal_column("result_revision", type_=String())).where(
                    bet.usuario_id == uid, bet.id.in_([a.id for a in records])
                )
            )
        ).all()
    }
    data = []
    for row in records:
        state, _ = projetar([
            (e.tipo, e.fonte, e.payload_json) for e in histories.get(row.chave or "", [])
        ])
        data.append(
            await view(session, row, state, contexts.get(row.id, {}), str(revisions[row.id]))
        )
    cursor = None
    if more:
        last = records[-1]
        frontier["last"] = [(last.data_jogo or last.criada_em).isoformat(), last.id]
        # Preserve the original token timestamp: pagination does not extend its validity.
        cursor = (
            cipher()
            .encrypt_at_time(
                json.dumps(frontier).encode(),
                int(datetime.fromisoformat(frontier["started"]).timestamp()),
            )
            .decode()
        )
    return {
        "data": data,
        "next_cursor": cursor,
        "total_elegivel": total,
        "observado_em": instant,
        "fronteira_iniciada_em": frontier["started"],
        "frescor": "primary_snapshot",
        "populacao": "pending_selected_financial_assigned_without_severe_review",
    }


async def operation(session: AsyncSession, uid: int, ident: UUID) -> ResultOperation | None:
    return cast(
        ResultOperation | None,
        await session.scalar(
            select(ResultOperation).where(
                ResultOperation.usuario_id == uid, ResultOperation.id == ident
            )
        ),
    )


def read_receipt(record: ResultOperation) -> dict[str, Any]:
    if record.created_at + timedelta(days=READ_DAYS) <= now():
        raise ResultError("result_receipt_expired", 410)
    return record.receipt


async def recover(session: AsyncSession, uid: int, ident: UUID) -> dict[str, Any]:
    record = await operation(session, uid, ident)
    if record is None:
        raise ResultError("result_intention_not_found", 404)
    return read_receipt(record)


async def mutate(
    session: AsyncSession, user: Usuario, ident: UUID, payload: ResultInput | UndoInput
) -> dict[str, Any]:
    from bancaemdia.domain.access import require_write_access
    from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

    await CruzamentoCandidatoRepo().lock(session, user.id)
    await require_write_access(session, user.id)
    kind = "result" if isinstance(payload, ResultInput) else "undo"
    fingerprint = digest({"kind": kind, "input": payload.model_dump(mode="json")})
    previous = await operation(session, user.id, ident)
    if previous is not None:
        if previous.kind != kind or previous.fingerprint != fingerprint:
            raise ResultError("result_intention_conflict")
        return read_receipt(previous)
    target = None
    if isinstance(payload, ResultInput):
        key = payload.chave
    else:
        target = await operation(session, user.id, payload.operacao_id)
        if target is None or target.kind != "result":
            raise ResultError("result_intention_not_found", 404)
        read_receipt(target)
        if target.created_at + timedelta(hours=UNDO_HOURS) <= now():
            raise ResultError("result_undo_expired", 410)
        if payload.revisao_esperada != UUID(target.receipt["aposta"]["revisao"]):
            raise ResultError("result_revision_conflict")
        key = str(target.receipt["aposta"]["chave"])
    if not await _travar(session, user.id, key):
        raise ResultError("result_busy")
    bet = await ApostaRepo().get_by_chave_for_update(session, user.id, key)
    if bet is None:
        raise ResultError("result_not_found", 404)
    if await revision(session, user.id, key) != str(payload.revisao_esperada):
        raise ResultError("result_revision_conflict")
    if await blocked(session, bet) is not None:
        raise ResultError("result_not_eligible", 422)
    history = await _historico(session, user.id, key)
    state, _ = projetar(history)
    if (
        state.get("estado", "PENDENTE") != bet.estado
        or state.get("retorno_centavos") != bet.retorno_centavos
    ):
        raise ResultError("result_projection_conflict")
    before = {field: state[field] for field in FINANCIAL_FIELDS if field in state}
    if isinstance(payload, ResultInput):
        # This protected operation also permits intentional correction of settled results.
        try:
            validar_resultado(
                payload.estado,
                payload.retorno_centavos,
                payload.cashout_valor_centavos,
                payload.comissao_centavos,
            )
        except ApostaInvalidaError:
            raise ResultError("result_invalid", 422) from None
        paid = (
            payload.cashout_valor_centavos
            if payload.cashout_valor_centavos is not None
            else payload.retorno_centavos
        )
        new = eventos_do_resultado(payload.estado, paid, payload.comissao_centavos)
    else:
        assert target is not None
        new = [
            EventoNovo(
                "RESULTADO_DESFEITO",
                "manual",
                {
                    "operacao_desfeita": str(target.id),
                    "ator_id": str(user.id),
                    "restauracao": target.before,
                },
            )
        ]
    after, _ = projetar([*history, *((e.tipo, e.fonte, e.payload) for e in new)])
    for event in new:
        event.payload.update(operacao_id=str(ident), ator_id=str(user.id))
    paid_return = after.get("retorno_centavos")
    if paid_return is not None and not 0 <= paid_return <= 2**63 - 1:
        raise ResultError("result_invalid", 422)
    recorded = await _aplicar(session, user, key, new, after)
    if recorded is None:
        raise ResultError("result_busy")
    instant = now()
    receipt: dict[str, Any] = jsonable_encoder({
        "id": ident,
        "tipo": kind,
        "aposta": await view(session, recorded, after),
        "operacao_desfeita": None if target is None else target.id,
        "confirmado_em": instant,
        "desfazer_ate": instant + timedelta(hours=UNDO_HOURS) if kind == "result" else None,
        "consultar_ate": instant + timedelta(days=READ_DAYS),
        "eventos_gravados": 1,
    })
    session.add(
        ResultOperation(
            usuario_id=user.id,
            id=ident,
            aposta_id=bet.id,
            kind=kind,
            fingerprint=fingerprint,
            before=before,
            receipt=receipt,
            created_at=instant,
        )
    )
    await session.flush()
    return receipt
