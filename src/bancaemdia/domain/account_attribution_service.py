"""Default account by game date; explicit multi-account identity takes precedence."""

from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.account_attribution import (
    AccountResolution,
    AccountUsage,
    ResolutionStatus,
    resolve_account,
)
from bancaemdia.domain.cruzamento import instant as parse_instant
from bancaemdia.domain.materializar import casa_canonica


def game_instant(state: dict[str, object]) -> datetime | None:
    """Never substitute posting, placement, capture or ingestion for the game clock."""
    return parse_instant(state.get("data_jogo") or state.get("comeca_em"))


async def account_for_state(
    session: AsyncSession, user: int, state: dict[str, object], *, lock: bool = True
) -> AccountResolution:
    reference = state.get("conta_casa_ref")
    if reference is None and state.get("conta_atribuicao") == "explicit":
        reference = state.get("conta_casa_id")
    if reference is not None and (not isinstance(reference, int) or isinstance(reference, bool)):
        raise InvalidAccountReferenceError("referência explícita inválida")
    casa = state.get("casa")
    return await attribute_account(
        session,
        user,
        casa if isinstance(casa, str) else None,
        game_instant(state),
        reference,
        lock=lock,
    )


async def account_review(
    session: AsyncSession, user: int, key: str, result: AccountResolution, *, invalid: bool = False
) -> None:
    from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo

    if result.status == ResolutionStatus.UNIQUE and not invalid:
        return
    reason = (
        "referência explícita inválida" if invalid else "conta ausente ou ambígua na data do jogo"
    )
    repo = RevisaoPendenteRepo()
    if not await repo.has_open(session, user, key, "conta_pendente"):
        await repo.create(
            session,
            {
                "usuario_id": user,
                "motivo": "conta_pendente",
                "extracao_bruta": {
                    "aposta_chave": key,
                    "razao": reason,
                    "status": str(result.status),
                },
            },
        )


class InvalidAccountReferenceError(ValueError):
    pass


async def attribute_account(
    session: AsyncSession,
    usuario_id: int,
    casa: str | None,
    instant: datetime | None,
    explicit_id: int | None = None,
    *,
    lock: bool = True,
) -> AccountResolution:
    nome = None if casa is None else casa_canonica(casa) or casa
    house_id = await session.scalar(select(models.Casa.id).where(models.Casa.nome == nome))
    if explicit_id is not None and (
        not isinstance(explicit_id, int)
        or isinstance(explicit_id, bool)
        or not 1 <= explicit_id <= 9223372036854775807
    ):
        raise InvalidAccountReferenceError("referência explícita inválida")
    statement = (
        select(models.ContaCasa)
        .where(
            models.ContaCasa.usuario_id == usuario_id,
            models.ContaCasa.casa_id == house_id,
        )
        .order_by(models.ContaCasa.id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(read=True)
    accounts = list(await session.scalars(statement))
    # #94 separates stable identity from usage. Prefer its published table whenever present;
    # main's desde/ate are the legacy usage intervals, including closed historical accounts.
    temporal = await session.scalar(text("SELECT to_regclass('public.usos_conta_casa')"))
    if temporal:
        rows = await session.execute(
            text(
                "SELECT conta_casa_id, vigente_de, vigente_ate FROM usos_conta_casa "
                "WHERE usuario_id=:user AND casa_id=:house ORDER BY id"
                + (" FOR SHARE" if lock else "")
            ),
            {"user": usuario_id, "house": house_id},
        )
        usages = [AccountUsage(*row) for row in rows]
    else:
        usages = [AccountUsage(account.id, account.desde, account.ate) for account in accounts]
    if explicit_id is not None:
        valid = {account.id for account in accounts}
        if explicit_id not in valid:
            raise InvalidAccountReferenceError(
                "referência de conta inválida para este usuário/casa"
            )
        # The actual account that placed a multi-account bet need not be the default at game time.
        return AccountResolution(ResolutionStatus.UNIQUE, explicit_id)
    return resolve_account(instant, usages)
