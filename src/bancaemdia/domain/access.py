from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.billing import AccessMode
from bancaemdia.repositories.assinatura_repo import AssinaturaRepo

# Evidence reading is explicitly available independently of write entitlement.
READ_OPERATION_MODES = {"aposta_foto": frozenset({AccessMode.FULL_WRITE, AccessMode.READ_ONLY})}


async def require_read_operation(session: AsyncSession, usuario_id: int, operation: str) -> None:
    modes = READ_OPERATION_MODES[operation]
    read = await AssinaturaRepo().read_status(session, usuario_id)
    if read.access not in modes:
        raise AccountReadOnlyError("account_read_only")


class AccountReadOnlyError(Exception):
    pass


# Controles de identidade e privacidade continuam possíveis em READ_ONLY: vincular/desvincular o
# Telegram, parear/revogar/girar instalação e excluir a conta.
CONTROL_OPERATIONS = frozenset({
    ("POST", "/api/v1/telegram/link-codes"),
    ("DELETE", "/api/v1/telegram/link"),
    ("DELETE", "/api/v1/usuario/me"),
    ("POST", "/api/v1/coleta/pairing-codes"),
})


def requires_full_write(method: str, path: str) -> bool:
    """Regra única do gate comercial das rotas do usuário; o OpenAPI publica a mesma decisão."""
    control_operation = (method, path) in CONTROL_OPERATIONS
    installation_path = path.split("/")
    if installation_path[:5] == ["", "api", "v1", "coleta", "installations"]:
        control_operation |= (
            len(installation_path) == 6 and installation_path[5].isdigit() and method == "DELETE"
        ) or (
            len(installation_path) == 7
            and installation_path[5].isdigit()
            and installation_path[6] == "rotate"
            and method == "POST"
        )
    return (
        not control_operation
        and method not in {"GET", "HEAD", "OPTIONS"}
        and not path.startswith("/api/v1/billing/")
    )


async def require_write_access(session: AsyncSession, usuario_id: int) -> None:
    read = await AssinaturaRepo().read_status(session, usuario_id)
    if read.access != AccessMode.FULL_WRITE:
        raise AccountReadOnlyError("account_read_only")


async def require_worker_write_access(usuario_id: int) -> None:
    from bancaemdia.workers.materialization import get_engine

    async with AsyncSession(get_engine()) as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(usuario_id)}
        )
        await require_write_access(session, usuario_id)
