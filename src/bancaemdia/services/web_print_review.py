"""Resolve a web review without first inventing a zero-valued financial bet."""

import math
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.v1 import apostas as apostas_api
from bancaemdia.domain.materializar import NovaAposta
from bancaemdia.domain.revisao_service import (
    ResolucaoInvalidaError,
    evento_da_resolucao,
    planejar_resolucao,
)
from bancaemdia.models.web_print import PrintAsset
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.workers.materialization import _gravar


async def resolve(
    session: AsyncSession, user: Any, review: Any, action: str, corrections: dict[str, Any] | None
) -> dict[str, Any]:
    raw = review.extracao_bruta or {}
    try:
        identity = UUID(raw["web_print_id"])
        ordinal = int(raw["ordem"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(409, "a proveniência desta revisão está incompleta") from None
    asset = await session.get(PrintAsset, identity)
    key = f"web_print:{identity}:{ordinal}"
    if (
        asset is None
        or asset.usuario_id != user.id
        or asset.digest != review.midia_hash
        or key != raw.get("aposta_chave")
        or not any(review.id in item["review_ids"] for item in asset.results or [])
    ):
        raise HTTPException(409, "a proveniência desta revisão está incompleta")
    fields = dict(raw.get("campos") or {})
    try:
        plan = planejar_resolucao(action, corrections, fields)
    except ResolucaoInvalidaError as exc:
        raise HTTPException(422, str(exc)) from None
    updated = {**fields, **(corrections or {})}
    bet = None
    events = 0
    if action == "CORRIGIR":
        for name, minimum in (("stake_unidades", 0.000001), ("odd", 1.01)):
            value = updated.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < minimum
            ):
                raise HTTPException(
                    422, "complete stake e odd válidas antes de confirmar a revisão"
                )
        message = await apostas_api._conferir_ids(session, user.id, corrections or {})
        if message is not None:
            raise HTTPException(422, message)
        updated.update({
            "origem": "print",
            "chat_id": None,
            "message_id": None,
            "ordem_na_mensagem": ordinal,
            "revisao_grave": False,
            "revisao_motivo": None,
            "web_print_id": str(identity),
        })
        nova = NovaAposta(
            key,
            ordinal,
            "print",
            None,
            None,
            None,
            updated.get("casa"),
            updated.get("data_aposta"),
            None,
            False,
            1.0,
            updated,
        )
        saved = await _gravar(
            session, user.id, nova, raw.get("extracao") or {}, asset.digest, manual_source=True
        )
        events += saved.eventos
        row = await ApostaRepo().get_by_chave(session, user.id, key)
        assert row is not None
        history = await apostas_api._historico(session, user.id, key)
        from bancaemdia.domain.materializar import projetar

        bet = apostas_api.linha_da_aposta(row, projetar(history)[0])
        asset.results = [
            {**item, "bet_key": key} if review.id in item["review_ids"] else item
            for item in asset.results or []
        ]
    elif action != "DESCARTAR":
        raise HTTPException(422, "esta revisão não tem um par para confirmar")
    event = evento_da_resolucao(review.id, action, review.motivo, plan.campos_corrigidos)
    await EventoRepo().append(
        session,
        {
            "usuario_id": user.id,
            "tipo": event.tipo,
            "fonte": event.fonte,
            "payload_json": event.payload,
            "aposta_chave": key,
            "chat_id": None,
            "message_id": None,
            "confianca": None,
        },
    )
    closed = await RevisaoPendenteRepo().resolve(session, user.id, review.id)
    assert closed is not None and closed.resolvido_em is not None
    return {
        "revisao": {"id": review.id, "resolvido_em": closed.resolvido_em.isoformat()},
        "acao": action,
        "aposta": bet,
        "eventos_gravados": events + 1,
    }
