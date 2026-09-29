"""Rebuild durable relations from append-only decisions, never from live candidates."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.cruzamento import instant


async def replay_relations(session: AsyncSession, user: int, *, dry_run: bool = False) -> int:
    events = await session.scalars(
        select(models.Evento)
        .where(
            models.Evento.usuario_id == user,
            models.Evento.tipo.in_([
                "APOSTAS_CONSOLIDADAS",
                "CONSOLIDACAO_DESVINCULADA",
                "CONSOLIDACAO_REJEITADA",
            ]),
        )
        .order_by(models.Evento.id)
    )
    decisions: dict[int, dict[str, Any]] = {}
    for event in events:
        payload = event.payload_json
        if payload.get("usuario_id") != user or payload.get("contrato") != 1:
            raise ValueError("invalid consolidation replay contract")
        ident = payload["relacao_id"]
        if event.tipo in {"APOSTAS_CONSOLIDADAS", "CONSOLIDACAO_REJEITADA"}:
            if ident in decisions and decisions[ident]["evidencia"] != payload["evidencia"]:
                raise ValueError("divergent consolidation evidence")
            rejected = event.tipo == "CONSOLIDACAO_REJEITADA"
            decisions.setdefault(ident, {**payload, "estado": "rejected" if rejected else "active"})
        else:
            if ident not in decisions:
                raise ValueError("unlink without consolidation decision")
            decisions[ident].update(
                estado="unlinked",
                desvinculada_em=payload["desvinculada_em"],
                motivo=payload["motivo"],
            )
    restored = 0
    for ident, payload in decisions.items():
        existing = await session.scalar(
            select(models.ApostaConsolidacao)
            .where(
                models.ApostaConsolidacao.usuario_id == user,
                models.ApostaConsolidacao.id == ident,
            )
            .execution_options(populate_existing=True)
        )
        if existing is None:
            restored += 1
            if not dry_run:
                session.add(
                    models.ApostaConsolidacao(
                        id=ident,
                        usuario_id=user,
                        casa_aposta_id=payload["casa_aposta_id"],
                        telegram_aposta_id=payload["telegram_aposta_id"],
                        conta_casa_id=payload["conta_casa_id"],
                        estado=payload["estado"],
                        decisao=payload["decisao"],
                        versao=payload["versao"],
                        evidencia=payload["evidencia"],
                        contexto=payload["contexto"],
                        ator=payload["ator"],
                        criada_em=instant(payload["criada_em"]),
                        desvinculada_em=instant(payload.get("desvinculada_em")),
                        motivo_desvinculacao=payload.get("motivo"),
                    )
                )
        elif (
            existing.evidencia != payload["evidencia"]
            or existing.contexto != payload["contexto"]
            or existing.casa_aposta_id != payload["casa_aposta_id"]
            or existing.telegram_aposta_id != payload["telegram_aposta_id"]
        ):
            raise ValueError("consolidation projection differs from immutable decision")
        elif existing.estado != payload["estado"] and not dry_run:
            existing.estado = payload["estado"]
            existing.desvinculada_em = instant(payload.get("desvinculada_em"))
            existing.motivo_desvinculacao = payload.get("motivo")
    if not dry_run:
        await session.flush()
    return restored
