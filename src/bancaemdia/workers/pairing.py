"""Serialize automatic house/tip matching and retain its decision in events."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import Aposta
from bancaemdia.observability.tracing import custom_span, set_custom_span_attributes
from bancaemdia.repositories.evento_repo import EventoRepo

MAX_CANDIDATES = 500


async def _registrar(
    session: AsyncSession, usuario_id: int, chave: str, tipo: str, payload: dict[str, object]
) -> None:
    await EventoRepo().append(
        session,
        {
            "usuario_id": usuario_id,
            "tipo": tipo,
            "fonte": "casa",
            "payload_json": payload,
            "confianca": None,
            "chat_id": None,
            "message_id": None,
            "aposta_chave": chave,
        },
    )


async def _criacoes(
    session: AsyncSession, usuario_id: int, chaves: list[str]
) -> dict[str, dict[str, Any]]:
    if not chaves:
        return {}
    linhas = (
        await session.execute(
            select(models.Evento.aposta_chave, models.Evento.payload_json)
            .where(
                models.Evento.usuario_id == usuario_id,
                models.Evento.tipo == "APOSTA_CRIADA",
                models.Evento.aposta_chave.in_(chaves),
            )
            .order_by(models.Evento.id)
        )
    ).all()
    return {str(chave): payload for chave, payload in linhas if chave is not None}


async def confirmar_par(session: AsyncSession, usuario_id: int, uma: Any, outra: Any) -> None:
    """Keep the real house bet, exclude the tip and record both sides for replay."""
    if {uma.origem, outra.origem} not in ({"casa", "telegram"}, {"casa", "print"}):
        raise ValueError("o par precisa de uma aposta da casa e uma dica")
    casa, dica = (uma, outra) if uma.origem == "casa" else (outra, uma)
    if not casa.chave or not dica.chave:
        raise ValueError("o par precisa de duas chaves")
    if (casa.parceira_chave and casa.parceira_chave != dica.chave) or (
        dica.parceira_chave and dica.parceira_chave != casa.chave
    ):
        raise ValueError("uma aposta já está pareada com outra")
    tipster_id = casa.tipster_id or dica.tipster_id
    await session.execute(
        update(models.Aposta)
        .where(models.Aposta.usuario_id == usuario_id, models.Aposta.id == casa.id)
        .values(
            selecionada=True,
            duvida_de_par=False,
            parceira_chave=dica.chave,
            tipster_id=tipster_id,
            revisao_grave=False if casa.duvida_de_par else casa.revisao_grave,
        )
    )
    await session.execute(
        update(models.Aposta)
        .where(models.Aposta.usuario_id == usuario_id, models.Aposta.id == dica.id)
        .values(
            selecionada=False,
            duvida_de_par=False,
            parceira_chave=casa.chave,
            revisao_grave=False if dica.duvida_de_par else dica.revisao_grave,
        )
    )
    await _registrar(
        session,
        usuario_id,
        casa.chave,
        "CORRECAO_MANUAL",
        {
            "parceira_chave": dica.chave,
            "tipster_id": tipster_id,
            **(
                {"duvida_de_par": False, "revisao_grave": False, "revisao_motivo": None}
                if casa.duvida_de_par
                else {}
            ),
        },
    )
    await _registrar(
        session,
        usuario_id,
        casa.chave,
        "SELECAO_ALTERADA",
        {
            "selecionada": True,
            "duvida_de_par": False,
            "parceira_chave": dica.chave,
        },
    )
    await _registrar(
        session,
        usuario_id,
        dica.chave,
        "SELECAO_ALTERADA",
        {
            "selecionada": False,
            "duvida_de_par": False,
            "parceira_chave": casa.chave,
        },
    )
    if dica.duvida_de_par:
        await _registrar(
            session,
            usuario_id,
            dica.chave,
            "CORRECAO_MANUAL",
            {
                "revisao_grave": False,
                "revisao_motivo": None,
            },
        )
    from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

    repo = CruzamentoCandidatoRepo()
    await repo.lock(session, usuario_id)
    await repo.invalidate(session, usuario_id, [casa.id, dica.id])


async def parear_criacao(
    session: AsyncSession, usuario_id: int, nova: Aposta, estado: dict[str, Any]
) -> str:
    """Persist candidates only; consolidation belongs to the explicit review / #110."""
    from bancaemdia.services.cruzamento_candidatos import generate

    with custom_span("cruzamento.pareador", aposta_nova_id=nova.id) as span:
        verdict = await generate(session, usuario_id, nova, estado)
        set_custom_span_attributes(span, "cruzamento.pareador", resultado=verdict)
        return verdict
