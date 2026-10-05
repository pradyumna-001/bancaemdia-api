"""Database boundary for the pure account resolver."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
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


def game_instant(state: dict[str, object]) -> datetime | None:
    value = state.get("data_jogo") or state.get("comeca_em")
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))


def explicit_reference(state: dict[str, object]) -> int | None:
    value = state.get("conta_casa_ref")
    if value is None and (state.get("conta_referencia_explicita") or state.get("conta_atribuicao") == "explicit"):
        value = state.get("conta_casa_id")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise InvalidAccountReferenceError("referência explícita inválida")
    return value


async def attribute_account(
    session: AsyncSession,
    usuario_id: int,
    casa: str | None,
    instant: datetime | None,
    explicit_id: int | None = None,
    *,
    lock: bool = False,
) -> AccountResolution:
    if explicit_id is None and instant is None:
        return AccountResolution(ResolutionStatus.NONE)
    nome = None if casa is None else casa_canonica(casa) or casa
    casa_id = None if nome is None else await CasaRepo().get_id_by_nome(session, nome)
    if explicit_id is not None:
        explicit_reference({"conta_casa_ref": explicit_id})
        account = await ContaCasaRepo().get_by_id(session, usuario_id, explicit_id)
        if account is None or account.casa_id != casa_id:
            raise InvalidAccountReferenceError("conta_casa_id não pertence a você nesta casa")
        return AccountResolution(ResolutionStatus.UNIQUE, explicit_id)
    if casa_id is None:
        return AccountResolution(ResolutionStatus.NONE)
    if lock:
        # Caller holds tenant/key/bet locks first; account identity and usages follow by ID.
        await session.execute(select(models.ContaCasa.id).where(
            models.ContaCasa.usuario_id == usuario_id, models.ContaCasa.casa_id == casa_id
        ).order_by(models.ContaCasa.id).with_for_update(read=True))
        usages = list((await session.execute(select(models.UsoContaCasa).where(
            models.UsoContaCasa.usuario_id == usuario_id, models.UsoContaCasa.casa_id == casa_id
        ).order_by(models.UsoContaCasa.id).with_for_update(read=True))).scalars())
    else:
        usages = await UsoContaCasaRepo().list_by_house(session, usuario_id, casa_id)
    return resolve_account(
        instant,
        (AccountUsage(u.conta_casa_id, u.vigente_de, u.vigente_ate) for u in usages),
    )


async def account_for_state(
    session: AsyncSession, user: int, state: dict[str, object], *, lock: bool = True
) -> AccountResolution:
    casa = state.get("casa")
    return await attribute_account(
        session, user, casa if isinstance(casa, str) else None,
        game_instant(state), explicit_reference(state), lock=lock,
    )


async def account_review(
    session: AsyncSession, user: int, key: str, result: AccountResolution, *, invalid: bool = False
) -> None:
    from bancaemdia.domain.account_review import sync_account_review

    await sync_account_review(session, user, key, result,
        context={"referencia_invalida": True} if invalid else None)
