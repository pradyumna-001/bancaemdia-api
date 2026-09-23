"""Serialize automatic house/tip matching and retain its decision in events."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.pareador import comparar
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


async def parear_criacao(
    session: AsyncSession, usuario_id: int, nova: Aposta, estado: dict[str, Any]
) -> str:
    """Apply one safe verdict in the writer transaction; a tenant lock orders pairers."""
    if (
        nova.chave is None
        or nova.data_aposta is None
        or nova.origem not in {"casa", "telegram", "print"}
    ):
        return "nova"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"pareador:{usuario_id}"},
    )
    opostas = ["telegram", "print"] if nova.origem == "casa" else ["casa"]
    candidatas = (
        (
            await session.execute(
                select(models.Aposta)
                .where(
                    models.Aposta.usuario_id == usuario_id,
                    models.Aposta.id != nova.id,
                    models.Aposta.origem.in_(opostas),
                    or_(
                        models.Aposta.data_aposta.is_(None),
                        models.Aposta.data_aposta.between(
                            nova.data_aposta - timedelta(days=14),
                            nova.data_aposta + timedelta(days=14),
                        ),
                    ),
                )
                .order_by(models.Aposta.id)
                .limit(MAX_CANDIDATES + 1)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    with custom_span("cruzamento.pareador", aposta_nova_id=nova.id) as span:
        if len(candidatas) > MAX_CANDIDATES:
            veredicto = "duvida"
            motivo = "há apostas demais para confirmar este par automaticamente"
            parceira = None
        else:
            criacoes = await _criacoes(
                session, usuario_id, [str(a.chave) for a in candidatas if a.chave]
            )
            iguais: list[models.Aposta] = []
            duvidas: list[models.Aposta] = []
            for aposta in candidatas:
                if not aposta.chave or (not aposta.selecionada and not aposta.parceira_chave):
                    continue
                payload = criacoes.get(aposta.chave)
                if payload is None:
                    continue
                resultado = comparar(estado, payload)
                if resultado.resultado == "igual":
                    iguais.append(aposta)
                elif resultado.resultado == "duvida":
                    duvidas.append(aposta)
            if len(iguais) == 1 and not duvidas and not iguais[0].parceira_chave:
                veredicto, motivo, parceira = "igual", None, iguais[0]
            elif iguais or duvidas:
                veredicto = "duvida"
                motivo = "parece a mesma aposta que já existe; confirme o par na revisão"
                parceira = (iguais or duvidas)[0]
            else:
                veredicto, motivo, parceira = "nova", None, None
        set_custom_span_attributes(
            span,
            "cruzamento.pareador",
            aposta_existente_id=None if parceira is None else parceira.id,
            resultado=veredicto,
        )
        if veredicto == "nova":
            return veredicto
        if veredicto == "duvida":
            await session.execute(
                update(models.Aposta)
                .where(models.Aposta.usuario_id == usuario_id, models.Aposta.id == nova.id)
                .values(selecionada=False, duvida_de_par=True, revisao_grave=True)
            )
            await _registrar(
                session,
                usuario_id,
                nova.chave,
                "SELECAO_ALTERADA",
                {
                    "selecionada": False,
                    "duvida_de_par": True,
                },
            )
            await _registrar(
                session,
                usuario_id,
                nova.chave,
                "CORRECAO_MANUAL",
                {
                    "revisao_grave": True,
                    "revisao_motivo": motivo,
                },
            )
            estado.update(
                selecionada=False, duvida_de_par=True, revisao_grave=True, revisao_motivo=motivo
            )
            estado["parceira_suspeita"] = None if parceira is None else parceira.chave
            return veredicto
        assert parceira is not None and parceira.chave is not None
        await confirmar_par(session, usuario_id, nova, parceira)
        casa, dica = (nova, parceira) if nova.origem == "casa" else (parceira, nova)
        if nova.origem != "casa":
            estado.update(selecionada=False, parceira_chave=casa.chave)
        else:
            estado["parceira_chave"] = dica.chave
        return veredicto
