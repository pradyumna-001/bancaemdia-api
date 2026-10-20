"""Typed review evidence and one atomic, recoverable human decision.

The existing account, correction, consolidation and print services remain the
domain engines. This boundary supplies authorization, evidence CAS and receipts.
"""

import hmac
import math
import re
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import String, case, column, func, literal, or_, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.v1.apostas import _aplicar, _conferir_ids, _historico, _travar
from bancaemdia.domain.access import require_write_access
from bancaemdia.domain.account_attribution import AccountUsage, ResolutionStatus, resolve_account
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
    explicit_reference,
    game_instant,
)
from bancaemdia.domain.aposta_service import (
    CAMPOS_CORRIGIVEIS,
    ApostaInvalidaError,
    campos_ausentes_criacao,
    validar_correcao,
)
from bancaemdia.domain.consolidacao_aposta import (
    ConsolidacaoRecusadaError,
    consolidate,
    reject_pair,
)
from bancaemdia.domain.cruzamento import VERSION
from bancaemdia.domain.financeiro import Aposta as ApostaFinanceira
from bancaemdia.domain.materializar import (
    EventoNovo,
    casa_canonica,
    projetar,
)
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.review_contract import ReviewDecision, ReviewQuery, ReviewView
from bancaemdia.domain.revisao_service import (
    ResolucaoInvalidaError,
    chave_da_aposta,
    evento_da_resolucao,
    planejar_resolucao,
)
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.models.review_operation import ReviewOperation
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.services.results import digest, now

READ_DAYS = 90
CONTEXT_TABLES = (
    "contas_casa",
    "usos_conta_casa",
    "titulares",
    "bancas",
    "cruzamento_candidatos",
    "cruzamento_entradas",
    "aposta_consolidacoes",
    "print_assets",
    "revisao_pendente",
)


class ReviewFailureError(Exception):
    def __init__(self, code: str, status: int = 409, field: str | None = None):
        self.code, self.status, self.field = code, status, field


def safe_label(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    # Labels from the authorized evidence, never nested extraction/provider diagnostics.
    return re.sub(r"https?://\S+", "[referência omitida]", value[:512]).strip()


def family_expression() -> Any:
    r, b = models.RevisaoPendente, models.Aposta
    raw = r.extracao_bruta
    owned_bet = (
        select(b.id)
        .where(b.usuario_id == r.usuario_id, b.chave == raw["aposta_chave"].astext)
        .exists()
    )
    reading = (
        select(b.id)
        .where(
            b.usuario_id == r.usuario_id,
            b.chave == raw["aposta_chave"].astext,
            or_(
                b.revisao_grave,
                select(models.Evento.id)
                .where(
                    models.Evento.usuario_id == r.usuario_id,
                    models.Evento.aposta_chave == raw["aposta_chave"].astext,
                    models.Evento.tipo == "APOSTA_CRIADA",
                    models.Evento.payload_json["revisao_grave"].astext == "true",
                )
                .exists(),
            ),
        )
        .exists()
    )
    return case(
        (raw["tipo_revisao"].astext == "conta", "conta"),
        (r.motivo.in_(["cruzamento_candidato", "consolidacao_pendente"]), "consolidacao"),
        ((raw["web_print_id"].astext.is_not(None)) & ~owned_bet, "print_incompleto"),
        (raw["parceira_suspeita"].astext.is_not(None), "par_legado"),
        (
            (raw["tipo_revisao"].astext == "leitura")
            | (raw["tipo_revisao"].astext.is_(None) & reading),
            "leitura",
        ),
        else_="nao_suportada",
    )


async def evidence_token(session: AsyncSession, uid: int, rid: int) -> str:
    # Opaque, tenant/review-bound fingerprint. Database revisions cover nominal/ABA
    # updates by old clients and raw SQL. Aggregate all owner sources conservatively
    # so a new competitor cannot slip past the evidence the human saw.
    parts: list[object] = ["review-evidence-v1", uid, rid]
    fingerprints = []

    def table_fingerprint(table: Any, identity: Any, revision: Any) -> Any:
        values = sql_cast(identity, String) + literal(":") + sql_cast(revision, String)
        return (
            select(
                func.coalesce(
                    func.md5(func.string_agg(values, aggregate_order_by(literal(","), identity))),
                    "",
                )
            )
            .select_from(table)
            .where(table.c.usuario_id == uid)
        )

    bets = models.Aposta.__table__
    fingerprints.append(
        table_fingerprint(bets, bets.c.id, column("result_revision")).scalar_subquery()
    )
    for name in CONTEXT_TABLES:
        table = models.Base.metadata.tables[name]
        identity = table.c.aposta_id if name == "cruzamento_entradas" else table.c.id
        fingerprints.append(
            table_fingerprint(table, identity, column("review_revision")).scalar_subquery()
        )
    for name in ("casas", "tipsters", "apelidos", "mercados", "competicoes", "times", "esportes"):
        table = models.Base.metadata.tables[name]
        fingerprints.append(
            select(
                func.coalesce(
                    func.md5(
                        func.string_agg(
                            sql_cast(func.to_jsonb(table.table_valued()), String),
                            aggregate_order_by(literal(","), table.c.id),
                        )
                    ),
                    "",
                )
            )
            .select_from(table)
            .scalar_subquery()
        )
    # Preserve every ordered fingerprint and the token format. One statement
    # observes a coherent snapshot; only SQL construction is reused, never data.
    parts.extend((await session.execute(select(*fingerprints))).one())
    return digest(parts)


async def account_view(session: AsyncSession, uid: int, state: dict[str, Any]) -> dict[str, Any]:
    name = casa_canonica(state.get("casa")) or state.get("casa")
    house = (
        await session.scalar(select(models.Casa).where(models.Casa.nome == name))
        if isinstance(name, str)
        else None
    )
    instant = game_instant(state)
    try:
        explicit = explicit_reference(state)
        result = await attribute_account(session, uid, name, instant, explicit)
        status = result.status.value
    except InvalidAccountReferenceError:
        explicit, status = None, "INVALID"
    rows = (
        []
        if house is None
        else list(
            await session.scalars(
                select(models.ContaCasa)
                .where(models.ContaCasa.usuario_id == uid, models.ContaCasa.casa_id == house.id)
                .order_by(models.ContaCasa.id)
            )
        )
    )
    usages = (
        []
        if house is None
        else list(
            await session.scalars(
                select(models.UsoContaCasa).where(
                    models.UsoContaCasa.usuario_id == uid, models.UsoContaCasa.casa_id == house.id
                )
            )
        )
    )
    holders = {
        h.id: h
        for h in await session.scalars(
            select(models.Titular).where(
                models.Titular.usuario_id == uid,
                models.Titular.id.in_([a.titular_id for a in rows if a.titular_id is not None]),
            )
        )
    }
    candidates = []
    for a in rows:
        # Explicit multicontas is valid even for historical/closed/no-holder accounts;
        # the actual domain checks owner and house rather than current active status.
        eligible = await attribute_account(session, uid, name, instant, a.id)
        match = resolve_account(
            instant,
            [
                AccountUsage(u.conta_casa_id, u.vigente_de, u.vigente_ate)
                for u in usages
                if u.conta_casa_id == a.id
            ],
        )
        holder = None if a.titular_id is None else holders.get(a.titular_id)
        candidates.append({
            "id": str(a.id),
            "apelido": safe_label(a.apelido) or "Conta sem apelido",
            "estado": a.estado,
            "ativa": a.ativa,
            "titular": None
            if holder is None
            else {
                "id": str(holder.id),
                "nome": safe_label(holder.nome) or "Titular",
                "arquivado": holder.arquivado,
            },
            "elegivel_escolha_explicita": eligible.status == ResolutionStatus.UNIQUE,
            "coincide_no_dia_do_jogo": match.status == ResolutionStatus.UNIQUE,
        })
    return {
        "casa": safe_label(name),
        "casa_id": None if house is None else str(house.id),
        "instante": instant,
        "criterio": "multicontas_explicita" if explicit is not None else "data_jogo",
        "status": status,
        "conta_atual_id": None
        if state.get("conta_casa_id") is None
        else str(state["conta_casa_id"]),
        "candidatos": candidates,
        "encaminhamento": "informar_casa"
        if house is None
        else "escolher_conta"
        if candidates
        else "cadastrar_conta_na_casa",
    }


async def _owned_pair_bets(session: AsyncSession, uid: int, selector: Any) -> list[Any]:
    return list(
        await session.scalars(
            select(models.Aposta).where(models.Aposta.usuario_id == uid, selector)
        )
    )


async def pair_sources(
    session: AsyncSession, uid: int, review: Any, family: str
) -> tuple[list[Any], Any]:
    raw = review.extracao_bruta or {}
    pair = None
    if family == "consolidacao":
        ident = raw.get("candidato_id")
        if isinstance(ident, int) and not isinstance(ident, bool):
            pair = await session.scalar(
                select(models.CruzamentoCandidato).where(
                    models.CruzamentoCandidato.usuario_id == uid,
                    models.CruzamentoCandidato.id == ident,
                    models.CruzamentoCandidato.revisao_id == review.id,
                )
            )
        if pair is None or pair.versao != VERSION or pair.status not in {"probable", "exact"}:
            return [], None
        bets = await _owned_pair_bets(
            session, uid, models.Aposta.id.in_([pair.casa_aposta_id, pair.telegram_aposta_id])
        )
        house = next((b for b in bets if b.id == pair.casa_aposta_id), None)
        tip = next((b for b in bets if b.id == pair.telegram_aposta_id), None)
        if (
            house is None
            or tip is None
            or house.chave != raw.get("parceira_suspeita")
            or tip.chave != raw.get("aposta_chave")
        ):
            return [], None
    else:
        bets = await _owned_pair_bets(
            session,
            uid,
            models.Aposta.chave.in_([raw.get("aposta_chave"), raw.get("parceira_suspeita")]),
        )
        house = next((b for b in bets if b.origem == "casa"), None)
        tip = next((b for b in bets if b.origem in {"telegram", "telegram_bot", "print"}), None)
        if (
            house is None
            or tip is None
            or house.id == tip.id
            or not any(b.duvida_de_par for b in bets)
        ):
            return [], None
    if house.origem != "casa" or tip.origem not in {"telegram", "telegram_bot", "print"}:
        return [], None
    return [house, tip], pair


async def source_view(
    session: AsyncSession, bet: Any, role: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    history = await _historico(session, bet.usuario_id, bet.chave)
    state, _ = projetar(history)
    tipster_label = safe_label(state.get("tipster"))
    if tipster_label is None and bet.tipster_id is not None:
        tipster_label = safe_label(
            await session.scalar(
                select(models.Tipster.nome).where(models.Tipster.id == bet.tipster_id)
            )
        )
    known_stake = (
        isinstance(state.get("stake_unidades"), (int, float))
        and not isinstance(state.get("stake_unidades"), bool)
        and math.isfinite(float(state["stake_unidades"]))
        and state["stake_unidades"] > 0
    )
    return {
        "chave": bet.chave,
        "papel": role,
        "origem": bet.origem,
        "casa": safe_label(state.get("casa")),
        "evento": safe_label(state.get("evento")),
        "descricao": safe_label(state.get("descricao")),
        "mercado": safe_label(state.get("mercado_bruto")),
        "data_jogo": bet.data_jogo,
        "data_aposta": bet.data_aposta,
        "tipster": tipster_label,
        "tipster_id": None if bet.tipster_id is None else str(bet.tipster_id),
        "odd": bet.odd,
        "stake_unidades": bet.stake_unidades if known_stake else None,
        "stake_centavos": str(bet.stake_centavos) if known_stake else None,
        "valor_aposta_centavos": str(bet.valor_aposta_centavos) if known_stake else None,
        "retorno_centavos": None if bet.retorno_centavos is None else str(bet.retorno_centavos),
        "freebet": bet.freebet,
        "selecionada": bet.selecionada,
    }, state


async def projection_agrees(session: AsyncSession, uid: int, key: str) -> bool:
    bet = await ApostaRepo().get_by_chave(session, uid, key)
    history = await _historico(session, uid, key)
    if bet is None or not history or history[0][0] != "APOSTA_CRIADA":
        return False
    state, _ = projetar(history)
    try:
        financial = ApostaFinanceira(
            float(state.get("stake_unidades") or 0.0),
            int(state.get("valor_unidade_centavos") or VALOR_UNIDADE_PADRAO_CENTAVOS),
            freebet=bool(state.get("freebet")),
        )
        return (
            financial.stake_centavos == bet.stake_centavos
            and financial.valor_aposta_centavos == bet.valor_aposta_centavos
            and financial.stake_unidades == bet.stake_unidades
            and financial.freebet == bet.freebet
            and state.get("odd") == bet.odd
            and state.get("estado", "PENDENTE") == bet.estado
            and state.get("retorno_centavos") == bet.retorno_centavos
        )
    except (TypeError, ValueError, OverflowError):
        return False


async def detail(session: AsyncSession, uid: int, rid: int) -> dict[str, Any]:
    review = await RevisaoPendenteRepo().get_by_id(session, uid, rid)
    if review is None:
        raise ReviewFailureError("review_not_found", 404)
    family = str(
        await session.scalar(
            select(family_expression()).where(
                models.RevisaoPendente.usuario_id == uid, models.RevisaoPendente.id == rid
            )
        )
    )
    raw, key = review.extracao_bruta or {}, chave_da_aposta(review.extracao_bruta)
    bet = None if key is None else await ApostaRepo().get_by_chave(session, uid, key)
    data: dict[str, Any] = {
        "id": str(rid),
        "familia": family,
        "motivo": {
            "leitura": "leitura_incompleta",
            "conta": "atribuicao_conta",
            "par_legado": "par_a_confirmar",
            "consolidacao": "fontes_a_consolidar",
            "print_incompleto": "print_sem_aposta",
            "nao_suportada": "motivo_nao_suportado",
        }[family],
        "estado": "aberta" if review.resolvido_em is None else "resolvida",
        "criado_em": review.criado_em,
        "resolvido_em": review.resolvido_em,
        "precondicao": await evidence_token(session, uid, rid),
        "acoes": [],
        "encaminhamento": "suporte",
        "resolver_url": None,
        "foto_url": f"/api/v1/revisao-operacional/revisoes/{rid}/foto"
        if review.midia_hash is not None and review.resolvido_em is None
        else None,
        "fontes": [],
        "conta": None,
        "par": None,
        "print_casa": None,
        "print_evento": None,
        "print_descricao": None,
        "print_odd": None,
        "print_stake_unidades": None,
    }
    if family in {"consolidacao", "par_legado"}:
        sources, pair = await pair_sources(session, uid, review, family)
        if sources:
            states = []
            for row, role in zip(sources, ("casa", "telegram"), strict=True):
                source, state = await source_view(session, row, role)
                data["fontes"].append(source)
                states.append(state)
            house, tip = sources
            options = [
                {
                    "fonte": role,
                    "rotulo": "Tipster da casa" if role == "casa" else "Tipster do Telegram",
                    "tipster": source["tipster"],
                    "tipster_id": source["tipster_id"],
                }
                for role, source in zip(("casa", "telegram"), data["fontes"], strict=True)
            ]
            conflicts = bool(
                (house.tipster_id and tip.tipster_id and house.tipster_id != tip.tipster_id)
                or (
                    states[0].get("tipster")
                    and states[1].get("tipster")
                    and states[0]["tipster"] != states[1]["tipster"]
                )
            )
            competitors = int(
                await session.scalar(
                    select(func.count())
                    .select_from(models.CruzamentoCandidato)
                    .where(
                        models.CruzamentoCandidato.usuario_id == uid,
                        models.CruzamentoCandidato.status.in_(["probable", "exact"]),
                        or_(
                            models.CruzamentoCandidato.casa_aposta_id == house.id,
                            models.CruzamentoCandidato.telegram_aposta_id == tip.id,
                        ),
                    )
                )
                or 0
            )
            data["par"] = {
                "tipo": "legado"
                if family == "par_legado"
                else "casa_financeira_telegram_contextual",
                "casa_chave": house.chave,
                "telegram_chave": tip.chave,
                "tipster_opcoes": options,
                "escolha_tipster_obrigatoria": conflicts,
                "concorrentes": max(0, competitors - 1),
                "busca_incompleta": bool(pair and pair.busca_truncada),
            }
            data["conta"] = await account_view(session, uid, states[0])
            active = await session.scalar(
                select(models.ApostaConsolidacao.id).where(
                    models.ApostaConsolidacao.usuario_id == uid,
                    models.ApostaConsolidacao.estado == "active",
                    or_(
                        models.ApostaConsolidacao.casa_aposta_id.in_([house.id, tip.id]),
                        models.ApostaConsolidacao.telegram_aposta_id.in_([house.id, tip.id]),
                    ),
                )
            )
            if not active:
                data.update(
                    acoes=["MESMA", "DISTINTAS", "RECUSAR_VINCULO"], encaminhamento="resolver_par"
                )
    elif bet is not None and family in {"leitura", "conta"}:
        source, state = await source_view(session, bet, "aposta")
        data["fontes"] = [source]
        if family == "conta":
            data["conta"] = await account_view(session, uid, state)
            recorded_status = raw.get("status")
            if recorded_status in {"NONE", "UNIQUE", "AMBIGUOUS"}:
                data["conta"]["status_na_abertura"] = recorded_status
            data.update(
                acoes=["CORRIGIR"] if data["conta"]["candidatos"] else [],
                encaminhamento="resolver_conta",
            )
        else:
            data.update(acoes=["CORRIGIR", "DESCARTAR"], encaminhamento="resolver_aposta")
    elif family == "print_incompleto":
        from bancaemdia.models.web_print import PrintAsset

        try:
            asset = await session.get(PrintAsset, UUID(str(raw.get("web_print_id"))))
        except ValueError:
            asset = None
        if (
            asset is not None
            and asset.usuario_id == uid
            and asset.digest == review.midia_hash
            and any(rid in item["review_ids"] for item in asset.results or [])
        ):
            fields = raw.get("campos") or {}
            data.update(
                acoes=["CORRIGIR", "DESCARTAR"],
                encaminhamento="resolver_print",
                print_casa=safe_label(fields.get("casa")),
                print_evento=safe_label(fields.get("evento")),
                print_descricao=safe_label(fields.get("descricao")),
                print_odd=fields.get("odd"),
                print_stake_unidades=fields.get("stake_unidades"),
            )
    if review.resolvido_em is not None:
        data.update(acoes=[], encaminhamento="resolvida")
    else:
        for source in data["fontes"]:
            if not await projection_agrees(session, uid, source["chave"]):
                data.update(acoes=[], encaminhamento="suporte")
                break
    if data["acoes"]:
        data["resolver_url"] = "/api/v1/revisao-operacional/operacoes/{operacao_id}"
    return ReviewView.model_validate(data).model_dump(mode="json")


async def queue(session: AsyncSession, uid: int, query: ReviewQuery) -> dict[str, Any]:
    r = models.RevisaoPendente
    base = select(r).where(r.usuario_id == uid, r.resolvido_em.is_(None))
    global_total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    if query.familia is not None:
        base = base.where(family_expression() == query.familia)
    if query.desde is not None:
        base = base.where(r.criado_em >= query.desde)
    if query.ate is not None:
        base = base.where(r.criado_em < query.ate)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    family = family_expression()
    counts = {
        str(f): int(n)
        for f, n in await session.execute(
            select(family, func.count()).where(*base._where_criteria).group_by(family)
        )
    }
    rows = list(
        await session.scalars(
            base
            .order_by(r.criado_em, r.id)
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
    )
    return {
        "data": [await detail(session, uid, r.id) for r in rows],
        "page": query.page,
        "page_size": query.page_size,
        "total_filtrado": total,
        "total_global": global_total,
        "por_familia_filtrado": counts,
        "observado_em": now(),
        "politica_consumo": "reiniciar_pagina_1_apos_resolver",
        "criterio_idade": "intervalo_criado_em",
    }


async def recover(session: AsyncSession, uid: int, ident: UUID) -> dict[str, Any]:
    op = await session.get(ReviewOperation, (uid, ident))
    if op is None:
        raise ReviewFailureError("review_intention_not_found", 404)
    if op.created_at + timedelta(days=READ_DAYS) <= now():
        raise ReviewFailureError("review_receipt_expired", 410)
    return op.receipt


def invalid_correction(message: str, corrections: dict[str, Any]) -> ReviewFailureError:
    # The internal message is never serialized; a bounded, published field name suffices.
    field = next(
        (name for name in corrections if name in CAMPOS_CORRIGIVEIS and name in message),
        "aposta_corrigida",
    )
    if "conta" in message:
        field = "conta_casa_id"
    return ReviewFailureError("review_correction_invalid", 422, field)


async def append_apply(
    session: AsyncSession, user: Usuario, key: str, events: list[EventoNovo]
) -> None:
    history = await _historico(session, user.id, key)
    if not history or history[0][0] != "APOSTA_CRIADA":
        raise ReviewFailureError("review_history_incomplete")
    state, _ = projetar([*history, *((e.tipo, e.fonte, e.payload) for e in events)])
    if await _aplicar(session, user, key, events, state) is None:
        raise ReviewFailureError("review_busy")
    if "duvida_de_par" in state:
        await session.execute(
            text("UPDATE apostas SET duvida_de_par=:v WHERE usuario_id=:u AND chave=:k"),
            {"v": bool(state["duvida_de_par"]), "u": user.id, "k": key},
        )


async def mutate(
    session: AsyncSession, user: Usuario, ident: UUID, payload: ReviewDecision
) -> dict[str, Any]:
    # Authentication may already have read the user. Start the decision on a fresh
    # serializable primary transaction, restoring the trusted tenant after rollback.
    await session.rollback()
    await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
    await session.execute(
        text("SELECT set_config('app.current_user_id',:u,true)"), {"u": str(user.id)}
    )
    if not await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k,0))"),
        {"k": f"pareador:{user.id}"},
    ):
        raise ReviewFailureError("review_busy")
    await require_write_access(session, user.id)
    fingerprint = digest({
        "contract": "review-v1",
        "payload": payload.model_dump(mode="json", exclude_unset=True),
    })
    previous = await session.get(ReviewOperation, (user.id, ident))
    if previous is not None:
        if previous.fingerprint != fingerprint:
            raise ReviewFailureError("review_intention_conflict")
        return await recover(session, user.id, ident)
    rid = int(payload.revisao_id)
    row = await session.scalar(
        select(models.RevisaoPendente)
        .where(models.RevisaoPendente.usuario_id == user.id, models.RevisaoPendente.id == rid)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ReviewFailureError("review_not_found", 404)
    if row.resolvido_em is not None:
        raise ReviewFailureError("review_already_resolved")
    view = await detail(session, user.id, rid)
    for source in sorted(view["fontes"], key=lambda s: s["chave"]):
        if not await _travar(session, user.id, source["chave"]):
            raise ReviewFailureError("review_busy")
        await session.execute(
            select(models.Aposta.id)
            .where(models.Aposta.usuario_id == user.id, models.Aposta.chave == source["chave"])
            .with_for_update(nowait=True)
        )
    if not hmac.compare_digest(view["precondicao"], payload.precondicao):
        raise ReviewFailureError("review_evidence_changed")
    if view["familia"] == "nao_suportada":
        raise ReviewFailureError("review_unsupported", 422)
    for source in view["fontes"]:
        if not await projection_agrees(session, user.id, source["chave"]):
            raise ReviewFailureError("review_projection_conflict")
    if payload.acao not in view["acoes"]:
        if view["familia"] in {"consolidacao", "par_legado"} and payload.acao in {
            "MESMA",
            "DISTINTAS",
            "RECUSAR_VINCULO",
        }:
            raise ReviewFailureError("review_link_incompatible")
        raise ReviewFailureError("review_action_not_allowed", 422, "acao")
    corrections = (
        {}
        if payload.aposta_corrigida is None
        else payload.aposta_corrigida.model_dump(exclude_unset=True)
    )
    if payload.conta_casa_id is not None:
        account_id = int(payload.conta_casa_id)
        if corrections.get("conta_casa_id", account_id) != account_id:
            raise ReviewFailureError("review_account_invalid", 422, "conta_casa_id")
        corrections["conta_casa_id"] = account_id
    if payload.acao != "CORRIGIR" and corrections:
        raise ReviewFailureError("review_correction_invalid", 422, "aposta_corrigida")
    if payload.tipster_choice is not None and payload.acao != "MESMA":
        raise ReviewFailureError("review_tipster_invalid", 422, "tipster_choice")
    key = chave_da_aposta(row.extracao_bruta)
    before_count = int(
        await session.scalar(
            select(func.count())
            .select_from(models.Evento)
            .where(models.Evento.usuario_id == user.id)
        )
        or 0
    )
    family = view["familia"]
    if family == "print_incompleto":
        from bancaemdia.services.web_print_review import resolve

        if not await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k,0))"),
            {"k": f"web-prints:{user.id}"},
        ):
            raise ReviewFailureError("review_busy")
        try:
            result = await resolve(session, user, row, payload.acao, corrections or None)
        except HTTPException as exc:
            raise (
                invalid_correction(str(exc.detail), corrections)
                if exc.status_code == 422
                else ReviewFailureError("review_link_incompatible")
            ) from None
        key = None if result["aposta"] is None else result["aposta"]["chave"]
    elif family in {"consolidacao", "par_legado"}:
        sources, pair = await pair_sources(session, user.id, row, family)
        if len(sources) != 2:
            raise ReviewFailureError("review_link_incompatible")
        house, tip = sources
        if payload.acao == "MESMA":
            if view["par"]["escolha_tipster_obrigatoria"] and payload.tipster_choice is None:
                raise ReviewFailureError("review_tipster_required", 422, "tipster_choice")
            if family == "par_legado":
                # Adopt the legacy pending pair into the shared durable fact model;
                # recover the retained tip before consolidation in this transaction.
                for source in sources:
                    await append_apply(
                        session,
                        user,
                        source.chave,
                        [
                            EventoNovo(
                                "SELECAO_ALTERADA",
                                "manual",
                                {"selecionada": True, "duvida_de_par": False},
                            )
                        ],
                    )
            try:
                await consolidate(
                    session,
                    user.id,
                    house.id,
                    tip.id,
                    decision="reviewed",
                    actor_id=user.id,
                    candidate_id=None if pair is None else pair.id,
                    tipster_choice=payload.tipster_choice,
                )
            except ConsolidacaoRecusadaError:
                raise ReviewFailureError("review_link_incompatible") from None
        else:
            try:
                await reject_pair(
                    session,
                    user.id,
                    house.id,
                    tip.id,
                    actor_id=user.id,
                    reason="decisão explícita pela revisão",
                )
            except ConsolidacaoRecusadaError:
                raise ReviewFailureError("review_link_incompatible") from None
            if family == "par_legado":
                for source in sources:
                    if source.duvida_de_par:
                        state, _ = projetar(await _historico(session, user.id, source.chave))
                        await append_apply(
                            session,
                            user,
                            source.chave,
                            list(planejar_resolucao("CORRIGIR", {}, state).eventos),
                        )
        key = house.chave if payload.acao == "MESMA" else key
    else:
        if key is None:
            raise ReviewFailureError("review_source_missing")
        history = await _historico(session, user.id, key)
        state, _ = projetar(history)
        if family == "conta" and corrections.get("conta_casa_id") is None:
            raise ReviewFailureError("review_account_required", 422, "conta_casa_id")
        message = await _conferir_ids(session, user.id, corrections)
        if message is not None:
            raise invalid_correction(message, corrections)
        try:
            plan = planejar_resolucao(payload.acao, corrections or None, state)
        except ResolucaoInvalidaError as exc:
            raise invalid_correction(str(exc), corrections) from None
        events = list(plan.eventos)
        if family == "conta":
            # Attribution must not silently approve an independent extraction review.
            events = [
                EventoNovo(
                    e.tipo,
                    e.fonte,
                    {
                        k: v
                        for k, v in e.payload.items()
                        if k not in {"revisao_motivo", "revisao_grave"}
                    },
                )
                if e.tipo == "CORRECAO_MANUAL"
                else e
                for e in events
            ]
            events = [e for e in events if e.payload and e.tipo != "SELECAO_ALTERADA"]
        if "conta_casa_id" in corrections:
            events.append(
                EventoNovo(
                    "CORRECAO_MANUAL",
                    "manual",
                    {
                        "conta_referencia_explicita": True,
                        "conta_casa_ref": corrections["conta_casa_id"],
                    },
                )
            )
        updated, _ = projetar([*history, *((e.tipo, e.fonte, e.payload) for e in events)])
        if family == "leitura" and payload.acao == "CORRIGIR":
            required = {name: updated.get(name) for name in ("casa", "odd", "stake_unidades")}
            missing = campos_ausentes_criacao(required)
            if missing:
                raise ReviewFailureError("review_correction_invalid", 422, missing[0])
            try:
                validar_correcao(required, state)
            except ApostaInvalidaError as exc:
                raise invalid_correction(str(exc), required) from None
        if any(f in corrections for f in ("conta_casa_id", "casa", "data_jogo")):
            try:
                account = await attribute_account(
                    session,
                    user.id,
                    updated.get("casa"),
                    game_instant(updated),
                    explicit_reference(updated),
                    lock=True,
                )
            except InvalidAccountReferenceError:
                raise ReviewFailureError("review_account_invalid", 422, "conta_casa_id") from None
            if family == "conta" and account.status != ResolutionStatus.UNIQUE:
                raise ReviewFailureError("review_account_required", 422, "conta_casa_id")
            events.append(
                EventoNovo("CORRECAO_MANUAL", "manual", {"conta_casa_id": account.conta_casa_id})
            )
        await append_apply(session, user, key, events)
    if family != "print_incompleto":
        event = evento_da_resolucao(rid, payload.acao, row.motivo, tuple(sorted(corrections)))
        event.payload["operacao_id"] = str(ident)
        from bancaemdia.repositories.evento_repo import EventoRepo

        await EventoRepo().append(
            session,
            {
                "usuario_id": user.id,
                "tipo": event.tipo,
                "fonte": "manual",
                "payload_json": event.payload,
                "aposta_chave": key,
                "chat_id": None,
                "message_id": None,
                "confianca": None,
            },
        )
        await RevisaoPendenteRepo().resolve(session, user.id, rid)
    closed = await RevisaoPendenteRepo().get_by_id(session, user.id, rid)
    if closed is None or closed.resolvido_em is None:
        raise ReviewFailureError("review_not_closed")
    instant = now()
    count = (
        int(
            await session.scalar(
                select(func.count())
                .select_from(models.Evento)
                .where(models.Evento.usuario_id == user.id)
            )
            or 0
        )
        - before_count
    )
    receipt = cast(
        dict[str, Any],
        jsonable_encoder({
            "operacao_id": ident,
            "revisao_id": str(rid),
            "familia": family,
            "acao": payload.acao,
            "aposta_chave": key,
            "confirmado_em": instant,
            "consultar_ate": instant + timedelta(days=READ_DAYS),
            "eventos_gravados": count,
        }),
    )
    session.add(
        ReviewOperation(
            usuario_id=user.id,
            id=ident,
            revisao_id=rid,
            fingerprint=fingerprint,
            receipt=receipt,
            created_at=instant,
        )
    )
    await session.flush()
    return receipt
