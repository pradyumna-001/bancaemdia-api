"""An explicit, transactional switch of the account used at one bookmaker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo

type EstadoAposTroca = Literal["DISPONIVEL", "LIMITADA", "ENCERRADA"]


class TrocaInvalidaError(ValueError):
    pass


class ChaveIdempotenciaEmConflitoError(TrocaInvalidaError):
    pass


@dataclass(frozen=True)
class TrocaPedido:
    casa_id: int
    conta_origem_id: int
    conta_destino_id: int
    efetiva_em: datetime
    estado_origem: EstadoAposTroca

    def normalizado(self) -> TrocaPedido:
        if self.efetiva_em.tzinfo is None or self.efetiva_em.utcoffset() is None:
            raise TrocaInvalidaError("efetiva_em deve ter fuso horário")
        if self.conta_origem_id == self.conta_destino_id:
            raise TrocaInvalidaError("origem e destino devem ser contas diferentes")
        if self.estado_origem not in {"DISPONIVEL", "LIMITADA", "ENCERRADA"}:
            raise TrocaInvalidaError("estado final da origem inválido")
        return TrocaPedido(
            self.casa_id,
            self.conta_origem_id,
            self.conta_destino_id,
            self.efetiva_em.astimezone(UTC),
            self.estado_origem,
        )


def _digest(pedido: TrocaPedido) -> str:
    payload = {
        "casa_id": pedido.casa_id,
        "conta_origem_id": pedido.conta_origem_id,
        "conta_destino_id": pedido.conta_destino_id,
        "efetiva_em": pedido.efetiva_em.isoformat(),
        "estado_origem": pedido.estado_origem,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def trocar_conta(
    session: AsyncSession,
    usuario_id: int,
    pedido: TrocaPedido,
    chave: str,
    *,
    aplicar: bool,
) -> dict[str, object]:
    """Serialize retries by tenant and commit only when the caller owns the session."""
    pedido = pedido.normalizado()
    tipo = "APPLY" if aplicar else "PREVIEW"
    digest = _digest(pedido)
    # The user row serializes concurrent requests, including requests with different keys.
    await session.execute(
        select(models.Usuario.id).where(models.Usuario.id == usuario_id).with_for_update()
    )
    existing = (
        await session.execute(
            select(models.TrocaTitularRequisicao).where(
                models.TrocaTitularRequisicao.usuario_id == usuario_id,
                models.TrocaTitularRequisicao.tipo == tipo,
                models.TrocaTitularRequisicao.chave == chave,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.pedido_hash != digest:
            raise ChaveIdempotenciaEmConflitoError("Idempotency-Key já usada com outro pedido")
        return existing.resposta
    if aplicar:
        preview = (
            await session.execute(
                select(models.TrocaTitularRequisicao).where(
                    models.TrocaTitularRequisicao.usuario_id == usuario_id,
                    models.TrocaTitularRequisicao.tipo == "PREVIEW",
                    models.TrocaTitularRequisicao.chave == chave,
                )
            )
        ).scalar_one_or_none()
        if preview is None or preview.pedido_hash != digest:
            raise TrocaInvalidaError("faça a prévia deste pedido com a mesma Idempotency-Key")
    now = await session.scalar(select(func.clock_timestamp()))
    if now is None or pedido.efetiva_em > now:
        raise TrocaInvalidaError("efetiva_em não pode estar no futuro")
    accounts = list(
        (
            await session.execute(
                select(models.ContaCasa)
                .where(
                    models.ContaCasa.usuario_id == usuario_id,
                    models.ContaCasa.id.in_((pedido.conta_origem_id, pedido.conta_destino_id)),
                )
                .order_by(models.ContaCasa.id)
                .with_for_update()
            )
        ).scalars()
    )
    if len(accounts) != 2 or any(a.casa_id != pedido.casa_id for a in accounts):
        raise TrocaInvalidaError("contas devem pertencer ao usuário e à mesma casa")
    source = next(a for a in accounts if a.id == pedido.conta_origem_id)
    target = next(a for a in accounts if a.id == pedido.conta_destino_id)
    usage = await UsoContaCasaRepo().current_for_update(session, usuario_id, pedido.casa_id)
    if usage is None or usage.conta_casa_id != source.id or source.estado != "EM_USO":
        raise TrocaInvalidaError("a origem não é a conta em uso")
    if usage.vigente_de is not None and pedido.efetiva_em <= usage.vigente_de:
        raise TrocaInvalidaError("efetiva_em deve ser posterior ao início do uso atual")
    if target.estado != "DISPONIVEL":
        raise TrocaInvalidaError("o destino deve estar disponível")
    conflicting_usage = await session.scalar(
        select(models.UsoContaCasa.id)
        .where(
            models.UsoContaCasa.usuario_id == usuario_id,
            models.UsoContaCasa.casa_id == pedido.casa_id,
            models.UsoContaCasa.id != usage.id,
            or_(
                models.UsoContaCasa.vigente_ate.is_(None),
                models.UsoContaCasa.vigente_ate > pedido.efetiva_em,
            ),
        )
        .limit(1)
    )
    if conflicting_usage is not None:
        raise TrocaInvalidaError("a troca sobrepõe outro período de uso")
    impacted = list(
        (
            await session.execute(
                select(models.Aposta.id)
                .where(
                    models.Aposta.usuario_id == usuario_id,
                    models.Aposta.conta_casa_id == source.id,
                    func.coalesce(models.Aposta.data_aposta, models.Aposta.criada_em)
                    >= pedido.efetiva_em,
                )
                .order_by(models.Aposta.id)
            )
        ).scalars()
    )
    result: dict[str, object] = {
        "casa_id": pedido.casa_id,
        "conta_origem_id": source.id,
        "conta_destino_id": target.id,
        "efetiva_em": pedido.efetiva_em.isoformat(),
        "estado_origem": pedido.estado_origem,
        "apostas_afetadas_ids": impacted,
        "aplicada": aplicar,
    }
    if aplicar:
        await UsoContaCasaRepo().close(session, usage.id, pedido.efetiva_em)
        await UsoContaCasaRepo().open(
            session, usuario_id, pedido.casa_id, target.id, pedido.efetiva_em
        )
        await session.execute(
            update(models.ContaCasa)
            .where(models.ContaCasa.id == source.id)
            .values(estado=pedido.estado_origem, ativa=False, ate=pedido.efetiva_em)
        )
        await session.execute(
            update(models.ContaCasa)
            .where(models.ContaCasa.id == target.id)
            .values(estado="EM_USO", ativa=True, desde=pedido.efetiva_em, ate=None)
        )
        # Preserve historical bet IDs and explicit account attribution. The impacted IDs
        # are returned so the user can review retrospective attribution before applying.
    request = (
        await session.execute(
            insert(models.TrocaTitularRequisicao)
            .values(
                usuario_id=usuario_id,
                tipo=tipo,
                chave=chave,
                pedido_hash=digest,
                resposta=result,
            )
            .returning(models.TrocaTitularRequisicao.id)
        )
    ).scalar_one()
    await session.execute(
        insert(models.TrocaTitularEvento).values(
            usuario_id=usuario_id,
            tipo=tipo,
            requisicao_id=request,
            dados={
                "casa_id": pedido.casa_id,
                "conta_origem_id": source.id,
                "conta_destino_id": target.id,
                "efetiva_em": pedido.efetiva_em.isoformat(),
                "estado_origem": pedido.estado_origem,
                "apostas_afetadas": len(impacted),
            },
        )
    )
    return result
