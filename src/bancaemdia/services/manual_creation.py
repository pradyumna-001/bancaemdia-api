"""Durable synchronous intent protocol around the existing manual creation service."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.access import require_write_access
from bancaemdia.domain.manual_contract import (
    ManualAssignmentNotice,
    ManualCode,
    ManualCreationReceipt,
    ManualCreationRequest,
    ManualField,
    ManualOriginalConfirmation,
)
from bancaemdia.models.manual_creation import ManualCreationOperation
from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

RETENTION = timedelta(days=90)


class ManualRefusedError(Exception):
    def __init__(self, code: ManualCode, status: int, field: ManualField | None = None) -> None:
        self.code = code
        self.status = status
        self.field = field


def fingerprint(request: ManualCreationRequest) -> str:
    values = request.model_dump(mode="json")
    # Missing/null commission means zero in the existing domain. Omitted dates
    # remain null: never hash a recomputed now(), unit, account, or registry value.
    values["comissao_centavos"] = request.comissao_centavos or "0"
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


async def now(session: AsyncSession) -> datetime:
    instant: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    return instant


async def find(session: AsyncSession, user: int, identity: UUID) -> ManualCreationOperation | None:
    result = await session.execute(
        select(ManualCreationOperation).where(
            ManualCreationOperation.usuario_id == user, ManualCreationOperation.id == identity
        )
    )
    return result.scalar_one_or_none()


async def recover(session: AsyncSession, user: int, identity: UUID) -> ManualCreationReceipt:
    row = await find(session, user, identity)
    if row is None:
        raise ManualRefusedError("manual_intention_not_found", 404)
    if await now(session) >= row.created_at + RETENTION:
        raise ManualRefusedError("manual_receipt_expired", 410)
    return ManualCreationReceipt.model_validate(row.receipt)


Creator = Callable[
    [], Awaitable[tuple[str, ManualOriginalConfirmation, list[ManualAssignmentNotice]]]
]


async def execute(
    session: AsyncSession,
    user: int,
    identity: UUID,
    request: ManualCreationRequest,
    creator: Creator,
) -> tuple[ManualCreationReceipt, bool]:
    # Reject read-only requests before contending for the owner's write lock.
    await require_write_access(session, user)
    # Same tenant/domain lock ordering as the existing writer. READ COMMITTED
    # sees a just-committed receipt after a concurrent first request releases it.
    await CruzamentoCandidatoRepo().lock(session, user)
    # Recheck current permission after waiting; the early check is not a grant.
    await require_write_access(session, user)
    digest = fingerprint(request)
    old = await find(session, user, identity)
    if old is not None:
        if old.fingerprint != digest:
            raise ManualRefusedError("manual_intention_conflict", 409)
        return await recover(session, user, identity), False

    key, original, notices = await creator()
    instant = await now(session)
    receipt = ManualCreationReceipt(
        intencao_id=identity,
        aposta_chave=key,
        detalhe_url="/api/v1/apostas/" + key,
        confirmado_em=instant,
        recuperavel_ate=instant + RETENTION,
        confirmacao_original=original,
        avisos=notices,
    )
    session.add(
        ManualCreationOperation(
            usuario_id=user,
            id=identity,
            fingerprint=digest,
            receipt=receipt.model_dump(mode="json"),
            created_at=instant,
        )
    )
    # A receipt failure rolls back the event, bet and account review as well.
    await session.flush()
    await session.commit()
    return receipt, True
