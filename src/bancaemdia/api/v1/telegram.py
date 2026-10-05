"""Authenticated account-link code and revocation endpoints."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user
from bancaemdia.db.session import get_db, get_db_primary
from bancaemdia.domain.registros import Usuario
from bancaemdia.services.telegram_link import CodeRateLimitError, get_link, issue_code, revoke_link

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)


class LinkCodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    expires_at: datetime


class LinkStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    linked: bool
    linked_at: datetime | None
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None


class RevocationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revoked: bool


@router.post(
    "/api/v1/telegram/link-codes",
    response_model=LinkCodeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Emitir código de vínculo Telegram",
    description="Emite um código de oito caracteres, válido por 30 minutos e exibido uma vez.",
)
async def criar_codigo(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    try:
        issued = await issue_code(session, usuario.id)
    except CodeRateLimitError as exc:
        raise HTTPException(status_code=429, detail="Tente novamente mais tarde") from exc
    await session.commit()
    return JSONResponse(
        LinkCodeResponse(code=issued.code, expires_at=issued.expires_at).model_dump(mode="json"),
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/api/v1/telegram/link",
    response_model=LinkStatusResponse,
    summary="Consultar vínculo Telegram",
    description="Consulta o estado do vínculo ativo da conta sem expor IDs do Telegram.",
)
async def consultar_vinculo(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> JSONResponse:
    link = await get_link(session, usuario.id)
    result = LinkStatusResponse(
        linked=link is not None,
        linked_at=None if link is None else link.linked_at,
        last_inbound_at=None if link is None else link.last_inbound_at,
        last_outbound_at=None if link is None else link.last_outbound_at,
    )
    return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.delete(
    "/api/v1/telegram/link",
    response_model=RevocationResponse,
    summary="Revogar vínculo Telegram",
    description="Revoga imediatamente o vínculo ativo e impede novo ingresso por esta identidade.",
)
async def revogar_vinculo(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    revoked = await revoke_link(session, usuario.id)
    await session.commit()
    return JSONResponse(
        RevocationResponse(revoked=revoked).model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )
