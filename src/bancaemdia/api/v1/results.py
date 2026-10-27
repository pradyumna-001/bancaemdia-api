"""Primary, private result queue and recoverable conditional mutations."""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse
from bancaemdia.api.deps import get_current_user, get_current_user_primary_snapshot
from bancaemdia.db.session import get_db, get_db_primary_snapshot
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.result_contract import (
    QueueFilter,
    QueuePage,
    ResultBet,
    ResultInput,
    ResultReceipt,
    UndoInput,
)
from bancaemdia.services import results as service

PREFIX = "/api/v1/resultados"
router = APIRouter(
    prefix=PREFIX,
    tags=["Resultados"],
    responses={
        **AUTHENTICATED_ERROR_RESPONSES,
        **{
            status: {
                "model": ErrorResponse,
                "description": "Código estável de recusa do fluxo de resultados",
            }
            for status in (403, 404, 409, 410, 429, 503)
        },
    },
)
ReadUser = Annotated[Usuario, Depends(get_current_user_primary_snapshot)]
ReadDB = Annotated[AsyncSession, Depends(get_db_primary_snapshot)]
User = Annotated[Usuario, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


async def run(session: AsyncSession, call: Awaitable[Any], *, write: bool = False) -> JSONResponse:
    headers = {
        "Cache-Control": "private, no-store",
        "Vary": "Cookie, Authorization",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        data = await call
        if write:
            await session.commit()
        return JSONResponse(jsonable_encoder(data), headers=headers)
    except service.ResultError as error:
        await session.rollback()
        if error.code == "result_busy":
            headers["Retry-After"] = "1"
        return JSONResponse({"detail": error.code}, status_code=error.status, headers=headers)
    except DBAPIError as error:
        await session.rollback()
        code = getattr(error.orig, "sqlstate", None)
        if code == "P0402":
            return JSONResponse({"detail": "account_read_only"}, status_code=402, headers=headers)
        headers["Retry-After"] = "5"
        return JSONResponse({"detail": "result_unavailable"}, status_code=503, headers=headers)


@router.get("/fila", response_model=QueuePage)
async def queue(
    query: Annotated[QueueFilter, Query()], usuario: ReadUser, session: ReadDB
) -> JSONResponse:
    return await run(session, service.queue(session, usuario.id, query))


@router.get("/apostas/{chave}", response_model=ResultBet)
async def detail(
    chave: Annotated[str, Path(min_length=1, max_length=512)], usuario: ReadUser, session: ReadDB
) -> JSONResponse:
    return await run(session, service.detail(session, usuario.id, chave))


@router.post("/operacoes/{intention_id}", response_model=ResultReceipt)
async def result(
    intention_id: UUID, pedido: ResultInput, usuario: User, session: DB
) -> JSONResponse:
    return await run(session, service.mutate(session, usuario, intention_id, pedido), write=True)


@router.post("/operacoes/{intention_id}/desfazer", response_model=ResultReceipt)
async def undo(intention_id: UUID, pedido: UndoInput, usuario: User, session: DB) -> JSONResponse:
    return await run(session, service.mutate(session, usuario, intention_id, pedido), write=True)


@router.get("/operacoes/{intention_id}", response_model=ResultReceipt)
async def recover(intention_id: UUID, usuario: ReadUser, session: ReadDB) -> JSONResponse:
    return await run(session, service.recover(session, usuario.id, intention_id))
