"""Queue unresolved account attribution without excluding the bet from user totals."""

from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.account_attribution import AccountResolution, review_reason
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo


async def sync_account_review(
    session: AsyncSession,
    usuario_id: int,
    aposta_chave: str,
    resolution: AccountResolution,
    *,
    midia_hash: str | None = None,
    context: dict[str, object] | None = None,
) -> str | None:
    reason = review_reason(resolution)
    repo = RevisaoPendenteRepo()
    # Only account reviews are superseded here; an unrelated extraction review stays open.
    for review in await repo.list_abertas_by_aposta_chave(session, usuario_id, aposta_chave):
        if (review.extracao_bruta or {}).get("tipo_revisao") == "conta" and review.motivo != reason:
            await repo.resolve(session, usuario_id, review.id)
    if reason is None or await repo.has_open(session, usuario_id, aposta_chave, reason):
        return None
    await repo.create(
        session,
        {
            "usuario_id": usuario_id,
            "midia_hash": midia_hash,
            "motivo": reason,
            "extracao_bruta": {
                "aposta_chave": aposta_chave,
                "tipo_revisao": "conta",
                "status": resolution.status.value,
                **(context or {}),
            },
        },
    )
    return reason
