"""Database boundary for the pure account resolver."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.account_attribution import (
    AccountResolution,
    AccountUsage,
    ResolutionStatus,
    resolve_account,
)
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.uso_conta_casa_repo import UsoContaCasaRepo


class InvalidAccountReferenceError(ValueError):
    pass


async def attribute_account(
    session: AsyncSession,
    usuario_id: int,
    casa: str | None,
    instant: datetime | None,
    explicit_id: int | None = None,
) -> AccountResolution:
    nome = None if casa is None else casa_canonica(casa) or casa
    casa_id = None if nome is None else await CasaRepo().get_id_by_nome(session, nome)
    if explicit_id is not None:
        account = await ContaCasaRepo().get_by_id(session, usuario_id, explicit_id)
        if account is None or account.casa_id != casa_id:
            raise InvalidAccountReferenceError("conta_casa_id não pertence a você nesta casa")
        return AccountResolution(ResolutionStatus.UNIQUE, explicit_id)
    if casa_id is None:
        return AccountResolution(ResolutionStatus.NONE)
    usages = await UsoContaCasaRepo().list_by_house(session, usuario_id, casa_id)
    return resolve_account(
        instant,
        (AccountUsage(u.conta_casa_id, u.vigente_de, u.vigente_ate) for u in usages),
    )
