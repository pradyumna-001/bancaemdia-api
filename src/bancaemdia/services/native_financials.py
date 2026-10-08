"""Exact, tenant-owned native ledger with conservative unversioned replay."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.coleta.readers.base import ReaderEnvelope
from bancaemdia.coleta.readers.native import NativeCanonicalBet, money_text
from bancaemdia.models import Casa, NativeAccount, NativeBet, NativeBetEvidence, Usuario


async def require_owner(session: AsyncSession, user_id: int) -> None:
    scoped = await session.scalar(
        text("SELECT NULLIF(current_setting('app.current_user_id', true), '')::bigint")
    )
    if scoped != user_id or not await session.scalar(
        select(Usuario.ativo).where(Usuario.id == user_id)
    ):
        raise PermissionError("authenticated active tenant scope required")


async def account_for_game(
    session: AsyncSession,
    user_id: int,
    house_id: int,
    bet: NativeCanonicalBet,
    *,
    explicit_account_id: int | None = None,
    multicontas: bool = False,
) -> NativeAccount | None:
    if multicontas != (explicit_account_id is not None):
        raise ValueError(
            "multicontas requires an explicit account; ordinary capture cannot override game attribution"
        )
    statement = select(NativeAccount).where(
        NativeAccount.usuario_id == user_id,
        NativeAccount.casa_id == house_id,
        NativeAccount.currency == bet.currency,
    )
    if explicit_account_id is not None:
        match = await session.scalar(statement.where(NativeAccount.id == explicit_account_id))
        if match is None:
            raise ValueError("invalid owned account, house or currency")
        return match
    candidates = list(
        (
            await session.scalars(
                statement.where(
                    NativeAccount.valid_from <= bet.game_at,
                    (NativeAccount.valid_to.is_(None)) | (NativeAccount.valid_to > bet.game_at),
                )
            )
        ).all()
    )
    return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True)
class NativeMaterialized:
    bet_id: int
    result: str
    needs_review: bool


async def materialize_native(
    session: AsyncSession,
    user_id: int,
    house_id: int,
    bet: NativeCanonicalBet,
    envelope: ReaderEnvelope,
    *,
    explicit_account_id: int | None = None,
    multicontas: bool = False,
) -> NativeMaterialized:
    await require_owner(session, user_id)
    bet = NativeCanonicalBet.model_validate(bet.model_dump(mode="json"))
    envelope = ReaderEnvelope.model_validate(envelope.model_dump(mode="json"))
    from bancaemdia.coleta.readers.one_win import OneWinReader

    if OneWinReader().parse_native(envelope).canonical_hash != bet.canonical_hash:
        raise ValueError("canonical bet does not match source envelope")
    if await session.scalar(select(Casa.nome).where(Casa.id == house_id)) != bet.brand:
        raise ValueError("house does not match exact source brand")
    # Per-identity transaction lock orders duplicate writers, not source versions.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"native:{user_id}:{bet.identity_hash}"},
    )
    account = await account_for_game(
        session,
        user_id,
        house_id,
        bet,
        explicit_account_id=explicit_account_id,
        multicontas=multicontas,
    )
    existing = await session.scalar(
        select(NativeBet)
        .where(
            NativeBet.usuario_id == user_id,
            NativeBet.identity_hash == bet.identity_hash,
        )
        .with_for_update()
    )
    values = bet.model_dump(mode="json")
    if existing is None:
        existing = NativeBet(
            usuario_id=user_id,
            account_id=None if account is None else account.id,
            currency=bet.currency,
            identity_hash=bet.identity_hash,
            canonical_hash=bet.canonical_hash,
            source_hash=envelope.content_hash,
            state=bet.state,
            stake=bet.stake,
            returned=bet.returned,
            game_at=bet.game_at,
            placed_at=bet.placed_at,
            needs_review=account is None,
            canonical=values,
        )
        session.add(existing)
        await session.flush()
        result = "created" if account is not None else "account_review"
    elif existing.canonical_hash != bet.canonical_hash or (
        explicit_account_id is not None and existing.account_id != explicit_account_id
    ):
        # No authoritative revision: never let last receipt overwrite a fact.
        # Exclude the entire identity from totals while its variants are reviewed.
        existing.needs_review = True
        result = "conflict_review"
    else:
        result = "noop"
    await session.execute(
        insert(NativeBetEvidence)
        .values(
            usuario_id=user_id,
            bet_id=existing.id,
            canonical_hash=bet.canonical_hash,
            source_hash=envelope.content_hash,
            captured_at=envelope.captured_at,
            canonical=values,
            envelope=envelope.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(constraint="uq_native_evidence_content")
    )
    await session.flush()
    return NativeMaterialized(existing.id, result, existing.needs_review)


async def native_summary(
    session: AsyncSession,
    user_id: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    account_id: int | None = None,
) -> list[dict[str, object]]:
    await require_owner(session, user_id)
    statement = select(
        NativeBet.currency,
        func.count(NativeBet.id),
        func.sum(NativeBet.stake),
        func.sum(NativeBet.returned),
        func.sum(NativeBet.returned - NativeBet.stake),
    ).where(
        NativeBet.usuario_id == user_id,
        NativeBet.needs_review.is_(False),
        NativeBet.account_id.is_not(None),
    )
    if since is not None:
        statement = statement.where(NativeBet.game_at >= since)
    if until is not None:
        statement = statement.where(NativeBet.game_at < until)
    if account_id is not None:
        statement = statement.where(NativeBet.account_id == account_id)
    rows = await session.execute(
        statement.group_by(NativeBet.currency).order_by(NativeBet.currency)
    )
    return [
        {
            "currency": currency,
            "bets": count,
            "stake": money_text(Decimal(stake)),
            "returned": money_text(Decimal(returned)),
            "profit": money_text(Decimal(profit)),
        }
        for currency, count, stake, returned, profit in rows
    ]
