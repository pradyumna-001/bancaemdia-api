"""The online and reviewed paths use the same durable consolidation service."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.registros import Aposta
from bancaemdia.observability.tracing import custom_span


async def confirmar_par(session: AsyncSession, usuario_id: int, uma: Any, outra: Any) -> None:
    from bancaemdia.domain.consolidacao_aposta import consolidate

    casa, dica = (uma, outra) if uma.origem == "casa" else (outra, uma)
    await consolidate(
        session, usuario_id, casa.id, dica.id, decision="reviewed", actor_id=usuario_id
    )


async def parear_criacao(
    session: AsyncSession, usuario_id: int, nova: Aposta, estado: dict[str, Any]
) -> str:
    from bancaemdia.domain.consolidacao_aposta import consolidate_available
    from bancaemdia.services.cruzamento_candidatos import generate

    with custom_span("cruzamento.pareador", usuario_id=usuario_id):
        verdict = await generate(session, usuario_id, nova, estado)
        await consolidate_available(session, usuario_id, nova.id)
    return verdict
