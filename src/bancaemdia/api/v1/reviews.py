"""Private operational review surface, compatible with the legacy routes."""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user, get_current_user_primary_snapshot
from bancaemdia.db.session import get_db, get_db_primary_snapshot
from bancaemdia.domain.foto_aposta import IMAGE_TYPES, InvalidPhotoError, validate_photo
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.review_contract import (
    ReviewDecision,
    ReviewError,
    ReviewPage,
    ReviewQuery,
    ReviewReceipt,
    ReviewView,
)
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.services import reviews as service

router = APIRouter(
    prefix="/api/v1/revisao-operacional",
    tags=["Revisão operacional"],
    responses={
        **AUTHENTICATED_ERROR_RESPONSES,
        **{
            code: {
                "model": ReviewError,
                "description": "Código público e campo seguro; sem detalhes internos.",
            }
            for code in (403, 404, 409, 410, 422, 503)
        },
    },
)
ReadUser = Annotated[Usuario, Depends(get_current_user_primary_snapshot)]
ReadDB = Annotated[AsyncSession, Depends(get_db_primary_snapshot)]
User = Annotated[Usuario, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


async def run(session: AsyncSession, call: Awaitable[Any], *, write: bool = False) -> JSONResponse:
    headers = {"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization"}
    try:
        data = await call
        if write:
            await session.commit()
        return JSONResponse(jsonable_encoder(data), headers=headers)
    except service.ReviewFailureError as exc:
        await session.rollback()
        if exc.code == "review_busy":
            headers["Retry-After"] = "1"
        return JSONResponse(
            {"detail": exc.code, "campo": exc.field}, status_code=exc.status, headers=headers
        )
    except DBAPIError as exc:
        await session.rollback()
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "P0402":
            return JSONResponse(
                {"detail": "account_read_only", "campo": None}, status_code=402, headers=headers
            )
        if sqlstate == "40001":
            return JSONResponse(
                {"detail": "review_evidence_changed", "campo": None},
                status_code=409,
                headers=headers,
            )
        if sqlstate in {"55P03", "40P01", "57014"}:
            headers["Retry-After"] = "1"
            return JSONResponse(
                {"detail": "review_busy", "campo": None}, status_code=409, headers=headers
            )
        headers["Retry-After"] = "5"
        return JSONResponse(
            {"detail": "review_unavailable", "campo": None}, status_code=503, headers=headers
        )


@router.get("/fila", response_model=ReviewPage)
async def queue(
    query: Annotated[ReviewQuery, Query()], usuario: ReadUser, session: ReadDB
) -> JSONResponse:
    return await run(session, service.queue(session, usuario.id, query))


@router.get("/revisoes/{revisao_id}", response_model=ReviewView)
async def detail(
    revisao_id: Annotated[int, Path(ge=1, le=2**63 - 1)], usuario: ReadUser, session: ReadDB
) -> JSONResponse:
    return await run(session, service.detail(session, usuario.id, revisao_id))


@router.post("/operacoes/{operacao_id}", response_model=ReviewReceipt)
async def decide(
    operacao_id: UUID, pedido: ReviewDecision, usuario: User, session: DB
) -> JSONResponse:
    return await run(session, service.mutate(session, usuario, operacao_id, pedido), write=True)


@router.get("/operacoes/{operacao_id}", response_model=ReviewReceipt)
async def recover(operacao_id: UUID, usuario: ReadUser, session: ReadDB) -> JSONResponse:
    return await run(session, service.recover(session, usuario.id, operacao_id))


@router.get(
    "/revisoes/{revisao_id}/foto",
    response_class=Response,
    responses={
        200: {
            "description": "Original privado, raster validado até 20 MiB/40 milhões de pixels/256 frames.",
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in IMAGE_TYPES.values()
            },
        }
    },
)
async def photo(
    revisao_id: Annotated[int, Path(ge=1, le=2**63 - 1)], usuario: ReadUser, session: ReadDB
) -> Response:
    import asyncio

    review = await RevisaoPendenteRepo().get_by_id(session, usuario.id, revisao_id)
    stored = await RevisaoPendenteRepo().get_foto_by_id(session, usuario.id, revisao_id)
    if review is None or review.resolvido_em is not None or not review.midia_hash or stored is None:
        return JSONResponse({"detail": "review_photo_not_found", "campo": None}, status_code=404)
    try:
        mime = await asyncio.to_thread(
            validate_photo, stored.conteudo, stored.tipo, review.midia_hash
        )
    except InvalidPhotoError:
        return JSONResponse({"detail": "review_photo_not_found", "campo": None}, status_code=404)
    return Response(
        stored.conteudo,
        media_type=mime,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
