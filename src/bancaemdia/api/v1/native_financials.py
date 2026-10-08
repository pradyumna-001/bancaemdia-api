"""Denominated account and report API; no implicit conversion to the BRL ledger."""

import json
from datetime import UTC, datetime
from decimal import localcontext
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    COLETA_ERROR_RESPONSES,
    ErrorResponse,
)
from bancaemdia.api.deps import get_current_user
from bancaemdia.api.v1.coleta import TOKEN_HEADER, hash_do_token
from bancaemdia.coleta.readers.base import ReaderEnvelope, StrictModel
from bancaemdia.coleta.readers.errors import ReaderError
from bancaemdia.coleta.readers.native import Currency, money_text
from bancaemdia.coleta.readers.native_admission import admitted
from bancaemdia.coleta.readers.one_win import OneWinReader
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db
from bancaemdia.domain.registros import Usuario
from bancaemdia.models import Casa, NativeAccount, NativeBet
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.coleta_instalacao import ColetaInstalacaoRepo, CollectionIdentity
from bancaemdia.services.native_financials import materialize_native, native_summary

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)
ID = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class NativeAccountCreate(StrictModel):
    casa_id: ID
    currency: Currency
    label: Annotated[str, Field(strict=True, min_length=1, max_length=120)]
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def period(self) -> "NativeAccountCreate":
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("account end must follow its start")
        return self


class NativeAccountResponse(NativeAccountCreate):
    id: int
    active: bool


class CurrencyTotals(StrictModel):
    currency: Currency
    bets: int
    stake: str
    returned: str
    profit: str


class NativeSummaryResponse(StrictModel):
    money_contract: Literal[2] = 2
    totals_by_currency: list[CurrencyTotals]
    combined_monetary_total: None = None


class NativeBetResponse(StrictModel):
    id: int
    account_id: int | None
    currency: Currency
    state: Literal["GREEN", "RED"]
    stake: str
    returned: str
    profit: str
    game_at: datetime
    placed_at: datetime
    needs_review: bool


class SourceTextEnvelope(ReaderEnvelope):
    """Distinct OpenAPI name preserves the existing collection-v2 ReaderEnvelope schema."""


class NativeCapture(StrictModel):
    transport_contract: Literal["reader-capture-1"]
    envelope: SourceTextEnvelope
    multicontas: Annotated[bool, Field(strict=True)] = False
    explicit_account_id: ID | None = None


class NativeCaptureResponse(StrictModel):
    bet_id: int
    result: Literal["created", "account_review", "noop", "conflict_review"]
    needs_review: bool


def unique_transport(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate transport key")
        result[key] = value
    return result


def reject_constant(value: str) -> object:
    raise ValueError("non-finite transport number")


@router.post(
    "/api/v1/financeiro/nativo/contas", response_model=NativeAccountResponse, status_code=201
)
async def create_account(
    payload: NativeAccountCreate,
    user: Usuario = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    if await session.scalar(select(Casa.id).where(Casa.id == payload.casa_id)) is None:
        raise HTTPException(404, "house not found")
    account = NativeAccount(usuario_id=user.id, **payload.model_dump(), active=True)
    session.add(account)
    await session.flush()
    response = {**payload.model_dump(), "id": account.id, "active": account.active}
    await session.commit()
    return response


@router.get("/api/v1/financeiro/nativo/contas", response_model=list[NativeAccountResponse])
async def list_accounts(
    user: Usuario = Depends(get_current_user), session: AsyncSession = Depends(get_db)
) -> list[dict[str, object]]:
    accounts = (
        await session.scalars(
            select(NativeAccount)
            .where(NativeAccount.usuario_id == user.id)
            .order_by(NativeAccount.id)
            .limit(100)
        )
    ).all()
    return [
        {
            "id": row.id,
            "casa_id": row.casa_id,
            "currency": row.currency,
            "label": row.label,
            "valid_from": row.valid_from,
            "valid_to": row.valid_to,
            "active": row.active,
        }
        for row in accounts
    ]


@router.get("/api/v1/financeiro/nativo/resumo", response_model=NativeSummaryResponse)
async def summary(
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    account_id: ID | None = None,
    user: Usuario = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    if since is not None and until is not None and until <= since:
        raise HTTPException(422, "end must follow start")
    return {
        "totals_by_currency": await native_summary(
            session, user.id, since=since, until=until, account_id=account_id
        )
    }


@router.get("/api/v1/financeiro/nativo/apostas", response_model=list[NativeBetResponse])
async def list_bets(
    limit: int = Query(default=50, ge=1, le=100),
    currency: Currency | None = None,
    user: Usuario = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, object]]:
    statement = select(NativeBet).where(NativeBet.usuario_id == user.id)
    if currency is not None:
        statement = statement.where(NativeBet.currency == currency)
    rows = (
        await session.scalars(
            statement.order_by(NativeBet.game_at.desc(), NativeBet.id.desc()).limit(limit)
        )
    ).all()
    with localcontext() as context:
        context.prec = 80
        return [
            {
                "id": row.id,
                "account_id": row.account_id,
                "currency": row.currency,
                "state": row.state,
                "stake": money_text(row.stake),
                "returned": money_text(row.returned),
                "profit": money_text(row.returned - row.stake),
                "game_at": row.game_at,
                "placed_at": row.placed_at,
                "needs_review": row.needs_review,
            }
            for row in rows
        ]


async def native_identity(
    request: Request, session: AsyncSession = Depends(get_db)
) -> CollectionIdentity:
    token = request.headers.get(TOKEN_HEADER)
    identity = (
        await ColetaInstalacaoRepo().authenticate(session, hash_do_token(token))
        if token and len(token) <= 128
        else None
    )
    if identity is None:
        raise HTTPException(403, "invalid collection installation credential")
    request.state.usuario_id = identity.usuario_id
    request.state.instalacao_id = identity.instalacao_id
    from bancaemdia.domain.access import require_write_access

    await require_write_access(session, identity.usuario_id)
    return identity


@router.post(
    "/api/v1/coleta/reader-captures",
    response_model=NativeCaptureResponse,
    responses={
        **COLETA_ERROR_RESPONSES,
        422: {"model": ErrorResponse},
        503: {
            "model": ErrorResponse,
            "description": "Native corpus admission is closed; retain capture without claiming ingestion.",
        },
    },
)
async def capture(
    payload: NativeCapture,
    request: Request,
    identity: CollectionIdentity = Depends(native_identity),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    repo = ColetaCasaRepo()
    await repo.lock_daily_admission(session, identity.usuario_id)
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    limit = get_settings().COLETA_DAILY_LIMIT
    if limit > 0 and await repo.count_received_since(session, identity.usuario_id, since) >= limit:
        raise HTTPException(429, "collection daily quota exceeded")
    # Production reader admission remains closed until its exact real corpus is reviewed.
    if not get_settings().ONE_WIN_NATIVE_ENABLED or not admitted():
        raise HTTPException(503, "native reader awaits reviewed corpus admission")
    house_id = await session.scalar(select(Casa.id).where(Casa.nome == "1win"))
    if house_id is None:
        raise HTTPException(409, "exact source house is not configured")
    try:
        # Reject ambiguous outer JSON too; the source remains its exact original text.
        json.loads(
            await request.body(), object_pairs_hook=unique_transport, parse_constant=reject_constant
        )
        bet = OneWinReader().parse_native(payload.envelope)
        result = await materialize_native(
            session,
            identity.usuario_id,
            house_id,
            bet,
            payload.envelope,
            explicit_account_id=payload.explicit_account_id,
            multicontas=payload.multicontas,
        )
    except ReaderError as error:
        from bancaemdia.services.reader_capture import quarantine_reader_error

        await quarantine_reader_error(session, identity.usuario_id, payload.envelope, error)
        await session.commit()
        raise HTTPException(422, f"{error.code.value}:{error.reason}") from None
    except (ValueError, RecursionError):
        raise HTTPException(422, "invalid source or account reference") from None
    await session.commit()
    return {"bet_id": result.bet_id, "result": result.result, "needs_review": result.needs_review}
