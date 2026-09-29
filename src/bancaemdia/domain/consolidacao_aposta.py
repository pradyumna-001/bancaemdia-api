"""Shared online/review application service. The caller owns commit and rollback."""

import copy
from datetime import UTC, datetime
from typing import Any, Literal

from prometheus_client import Counter
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.account_attribution import ResolutionStatus
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
)
from bancaemdia.domain.cruzamento import VERSION, instant
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.domain.projecao import projetar
from bancaemdia.repositories.aposta_consolidacao import ApostaConsolidacaoRepo
from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo
from bancaemdia.services.cruzamento_candidatos import generate

outcomes = Counter(
    "aposta_consolidation_total", "Consolidation attempts and outcomes", ["decision", "result"]
)


class ConsolidacaoRecusadaError(ValueError):
    """A domain refusal, safe to roll back or leave as a visible review."""


async def _sources(session: AsyncSession, user: int, ids: list[int]) -> list[models.Aposta]:
    await CruzamentoCandidatoRepo().lock(session, user)
    keys = list(
        await session.scalars(
            select(models.Aposta.chave)
            .where(
                models.Aposta.usuario_id == user,
                models.Aposta.id.in_(ids),
            )
            .order_by(models.Aposta.chave)
        )
    )
    for key in keys:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": f"{user}:{key}"}
        )
    return list(
        await session.scalars(
            select(models.Aposta)
            .where(
                models.Aposta.usuario_id == user,
                models.Aposta.id.in_(ids),
            )
            .order_by(models.Aposta.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


async def _state(session: AsyncSession, user: int, key: str | None) -> dict[str, Any]:
    history = (
        await session.execute(
            select(models.Evento.tipo, models.Evento.fonte, models.Evento.payload_json)
            .where(
                models.Evento.usuario_id == user,
                models.Evento.aposta_chave == key,
            )
            .order_by(models.Evento.id)
        )
    ).all()
    if not history or history[0].tipo != "APOSTA_CRIADA":
        raise ConsolidacaoRecusadaError("histórico da fonte incompleto")
    return projetar((kind, source, payload) for kind, source, payload in history)[0]


async def _audit(
    session: AsyncSession,
    relation: models.ApostaConsolidacao,
    kind: str,
    keys: list[str],
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "contrato": 1,
        "relacao_id": relation.id,
        "usuario_id": relation.usuario_id,
        "casa_aposta_id": relation.casa_aposta_id,
        "telegram_aposta_id": relation.telegram_aposta_id,
        "conta_casa_id": relation.conta_casa_id,
        "decisao": relation.decisao,
        "versao": relation.versao,
        "evidencia": relation.evidencia,
        "contexto": relation.contexto,
        "ator": relation.ator,
        "criada_em": relation.criada_em.isoformat(),
        **(extra or {}),
    }
    for key in keys:
        session.add(
            models.Evento(
                usuario_id=relation.usuario_id,
                tipo=kind,
                fonte="manual"
                if relation.decisao == "reviewed" or kind == "CONSOLIDACAO_DESVINCULADA"
                else "casa",
                aposta_chave=key,
                payload_json=payload,
            )
        )
    await session.flush()


async def consolidate(
    session: AsyncSession,
    user: int,
    house_id: int,
    telegram_id: int,
    *,
    decision: Literal["automatic", "reviewed"] = "automatic",
    actor_id: int | None = None,
    candidate_id: int | None = None,
    tipster_choice: Literal["casa", "telegram"] | None = None,
) -> models.ApostaConsolidacao:
    """Revalidate under user → source-key → ordered row locks; never commit here."""
    outcomes.labels(decision=decision, result="attempt").inc()
    if decision == "reviewed" and actor_id != user:
        raise ConsolidacaoRecusadaError("a decisão revisada exige o próprio usuário autenticado")
    bets = await _sources(session, user, [house_id, telegram_id])
    by_id = {bet.id: bet for bet in bets}
    house, tip = by_id.get(house_id), by_id.get(telegram_id)
    if (
        house is None
        or tip is None
        or house.origem != "casa"
        or tip.origem not in {"telegram", "print"}
    ):
        raise ConsolidacaoRecusadaError("fontes indisponíveis para este usuário")
    active = await ApostaConsolidacaoRepo().active(session, user, [house_id, telegram_id])
    if active:
        if (
            len(active) == 1
            and active[0].casa_aposta_id == house_id
            and active[0].telegram_aposta_id == telegram_id
        ):
            outcomes.labels(decision=decision, result="idempotent").inc()
            return active[0]
        raise ConsolidacaoRecusadaError("uma das fontes já pertence a outro fato")
    if any(not b.chave or not b.selecionada or b.duplicada_de or b.parceira_chave for b in bets):
        # Legacy pair adoption happens in migration, never by inventing a new automatic decision.
        raise ConsolidacaoRecusadaError("fonte excluída, duplicada ou pareada")
    states = [await _state(session, user, b.chave) for b in (house, tip)]
    hstate, tstate = states
    hname, tname = casa_canonica(hstate.get("casa")), casa_canonica(tstate.get("casa"))
    if not hname or hname != tname:
        raise ConsolidacaoRecusadaError("as fontes não pertencem à mesma casa")
    previous_unlink = await session.scalar(
        select(models.ApostaConsolidacao.id)
        .where(
            models.ApostaConsolidacao.usuario_id == user,
            models.ApostaConsolidacao.casa_aposta_id == house_id,
            models.ApostaConsolidacao.telegram_aposta_id == telegram_id,
            models.ApostaConsolidacao.estado == "unlinked",
        )
        .limit(1)
    )
    if previous_unlink and decision == "automatic":
        raise ConsolidacaoRecusadaError("par desvinculado exige nova decisão revisada")
    pair = await session.scalar(
        select(models.CruzamentoCandidato)
        .where(
            models.CruzamentoCandidato.usuario_id == user,
            models.CruzamentoCandidato.casa_aposta_id == house_id,
            models.CruzamentoCandidato.telegram_aposta_id == telegram_id,
            models.CruzamentoCandidato.versao == VERSION,
        )
        .execution_options(populate_existing=True)
    )
    if decision == "automatic":
        if (
            pair is None
            or pair.status != "exact"
            or pair.busca_truncada
            or (candidate_id is not None and pair.id != candidate_id)
        ):
            raise ConsolidacaoRecusadaError("candidato indisponível ou inelegível")
        evidence = copy.deepcopy(pair.evidencia)
        # Rebuild both directions from persistent events, physical values and current dictionary.
        # This also rechecks hidden saturation markers and every currently available competitor.
        for bet, state in zip((house, tip), states, strict=True):
            await generate(session, user, bet, state)
        await session.refresh(pair)
        if (
            pair.status != "exact"
            or pair.busca_truncada
            or any(evidence[side] != pair.evidencia[side] for side in ("casa", "telegram"))
        ):
            raise ConsolidacaoRecusadaError(
                "elegibilidade obsoleta, concorrente ou busca incompleta"
            )
    else:
        evidence = (
            copy.deepcopy(pair.evidencia) if pair is not None else {"revisao_sem_candidato": True}
        )
    game_time = instant(hstate.get("data_jogo") or hstate.get("comeca_em"))
    # Default account follows Casa's game clock; an explicit multi-account reference wins.
    explicit = hstate.get("conta_casa_ref")
    if explicit is None and hstate.get("conta_atribuicao") == "explicit":
        explicit = hstate["conta_casa_id"]
    try:
        account = await attribute_account(session, user, hname, game_time, explicit)
    except InvalidAccountReferenceError as error:
        raise ConsolidacaoRecusadaError(
            "referência explícita inválida; revisão necessária"
        ) from error
    if account.status != ResolutionStatus.UNIQUE:
        raise ConsolidacaoRecusadaError(
            "conta ausente ou ambígua na data do jogo; revisão necessária"
        )
    if (
        house.tipster_id
        and tip.tipster_id
        and house.tipster_id != tip.tipster_id
        and tipster_choice is None
    ):
        raise ConsolidacaoRecusadaError("tipsters conflitantes; escolha explícita necessária")
    chosen = tip.tipster_id if tipster_choice == "telegram" else house.tipster_id or tip.tipster_id
    context = {
        "tipster_id": chosen,
        "tipster_casa_id": house.tipster_id,
        "tipster_telegram_id": tip.tipster_id,
        "escolha": tipster_choice,
        "chat_id": tip.chat_id,
        "message_id": tip.message_id,
        "midia_hash": tip.midia_hash,
        "telegram_chave": tip.chave,
        "tipster_casa": hstate.get("tipster"),
        "tipster_telegram": tstate.get("tipster"),
    }
    if (
        hstate.get("tipster")
        and tstate.get("tipster")
        and hstate["tipster"] != tstate["tipster"]
        and tipster_choice is None
    ):
        raise ConsolidacaoRecusadaError(
            "contexto de tipster conflitante; escolha explícita necessária"
        )
    evidence.update({
        "fontes": {"casa": hstate, "telegram": tstate},
        "conta": {
            "referencia_explicita": explicit,
            "data_jogo": game_time.isoformat() if game_time else None,
            "conta_casa_id": account.conta_casa_id,
        },
        "valores_casa": {
            "stake_centavos": house.stake_centavos,
            "odd": house.odd,
            "estado": house.estado,
            "retorno_centavos": house.retorno_centavos,
        },
    })
    relation = models.ApostaConsolidacao(
        usuario_id=user,
        casa_aposta_id=house_id,
        telegram_aposta_id=telegram_id,
        conta_casa_id=account.conta_casa_id,
        estado="active",
        decisao=decision,
        versao=VERSION,
        evidencia=evidence,
        contexto=context,
        ator=f"usuario:{user}" if decision == "reviewed" else "worker",
    )
    try:
        async with session.begin_nested():
            session.add(relation)
            await session.flush()
    except IntegrityError as error:
        original = error.orig
        cause = getattr(original, "__cause__", None)
        constraint = getattr(cause, "constraint_name", None)
        if constraint in {"uq_consolidacao_casa_ativa", "uq_consolidacao_telegram_ativa"}:
            outcomes.labels(decision=decision, result="conflict").inc()
            raise ConsolidacaoRecusadaError("uma das fontes já pertence a outro fato") from error
        raise
    # A valid assignment is part of the canonical fact; original source fields remain in events.
    house.conta_casa_id = account.conta_casa_id
    house.tipster_id = chosen
    await _audit(session, relation, "APOSTAS_CONSOLIDADAS", [str(house.chave), str(tip.chave)])
    await CruzamentoCandidatoRepo().invalidate(session, user, [house_id, telegram_id])
    outcomes.labels(decision=decision, result="consolidated").inc()
    return relation


async def consolidate_available(session: AsyncSession, user: int, bet_id: int) -> None:
    pairs = list(
        await session.scalars(
            select(models.CruzamentoCandidato)
            .where(
                models.CruzamentoCandidato.usuario_id == user,
                models.CruzamentoCandidato.status == "exact",
                or_(
                    models.CruzamentoCandidato.casa_aposta_id == bet_id,
                    models.CruzamentoCandidato.telegram_aposta_id == bet_id,
                ),
            )
            .order_by(models.CruzamentoCandidato.id)
        )
    )
    for pair in pairs:
        pair_id, house_id, tip_id = pair.id, pair.casa_aposta_id, pair.telegram_aposta_id
        try:
            # Expected domain refusals are isolated without allowing partial domain writes.
            async with session.begin_nested():
                await consolidate(
                    session,
                    user,
                    house_id,
                    tip_id,
                    candidate_id=pair_id,
                )
        except ConsolidacaoRecusadaError as error:
            outcomes.labels(decision="automatic", result="review").inc()
            # Stable reasons only; evidence lives in the tenant-owned database, never labels/logs.
            reason = str(error)
            fresh_pair = await session.get(
                models.CruzamentoCandidato, pair_id, populate_existing=True
            )
            assert fresh_pair is not None
            pair = fresh_pair
            assert pair is not None
            house = await session.get(models.Aposta, pair.casa_aposta_id)
            tip = await session.get(models.Aposta, pair.telegram_aposta_id)
            assert house is not None and tip is not None
            if pair.revisao_id is None:
                review = models.RevisaoPendente(
                    usuario_id=user,
                    motivo="consolidacao_pendente",
                    extracao_bruta={
                        "candidato_id": pair.id,
                        "aposta_chave": tip.chave,
                        "parceira_suspeita": house.chave,
                        "razao": reason,
                    },
                )
                session.add(review)
                await session.flush()
                pair.revisao_id = review.id
            else:
                existing_review = await session.get(models.RevisaoPendente, pair.revisao_id)
                assert existing_review is not None
                review = existing_review
                review.resolvido_em = None
                review.extracao_bruta = {**(review.extracao_bruta or {}), "razao": reason}
            await session.flush()


async def unlink(
    session: AsyncSession, user: int, relation_id: int, *, actor_id: int, reason: str
) -> models.ApostaConsolidacao:
    if actor_id != user or not reason.strip() or len(reason) > 500:
        raise ConsolidacaoRecusadaError("desvinculação exige usuário e motivo válido")
    await CruzamentoCandidatoRepo().lock(session, user)
    relation = await session.scalar(
        select(models.ApostaConsolidacao)
        .where(
            models.ApostaConsolidacao.usuario_id == user,
            models.ApostaConsolidacao.id == relation_id,
        )
        .execution_options(populate_existing=True)
    )
    if relation is None:
        raise ConsolidacaoRecusadaError("relação indisponível")
    if relation.estado == "unlinked":
        return relation
    bets = await _sources(session, user, [relation.casa_aposta_id, relation.telegram_aposta_id])
    if len(bets) != 2:
        raise ConsolidacaoRecusadaError("fontes indisponíveis")
    relation.estado = "unlinked"
    relation.desvinculada_em = datetime.now(UTC)
    relation.motivo_desvinculacao = reason
    for bet in bets:
        if bet.id == relation.casa_aposta_id:
            bet.tipster_id = relation.contexto.get("tipster_casa_id")
    # Only legacy selection was used for suppression. Restore it explicitly and audit it.
    if relation.decisao == "legacy":
        for bet in bets:
            bet.parceira_chave = None
            if bet.id == relation.telegram_aposta_id:
                bet.selecionada = True
    await _audit(
        session,
        relation,
        "CONSOLIDACAO_DESVINCULADA",
        [str(b.chave) for b in bets],
        {
            "desvinculada_em": relation.desvinculada_em.isoformat(),
            "motivo": reason,
            "ator": f"usuario:{user}",
        },
    )
    await CruzamentoCandidatoRepo().invalidate(session, user, [b.id for b in bets])
    outcomes.labels(decision="reviewed", result="unlinked").inc()
    return relation
