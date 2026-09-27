from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.billing import AccessMode
from bancaemdia.repositories.assinatura_repo import AssinaturaRepo


class AccountReadOnlyError(Exception):
    pass


async def require_write_access(session: AsyncSession, usuario_id: int) -> None:
    read = await AssinaturaRepo().read_status(session, usuario_id)
    if read.access != AccessMode.FULL_WRITE:
        raise AccountReadOnlyError("account_read_only")


async def require_worker_write_access(usuario_id: int) -> None:
    from bancaemdia.domain.billing_checkout import tenant
    from bancaemdia.workers.materialization import get_engine

    async with AsyncSession(get_engine()) as session:
        await tenant(session, usuario_id)
        await require_write_access(session, usuario_id)
