from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    COLETA_ERROR_RESPONSES,
    ErrorResponse,
)
from bancaemdia.api.deps import _current_user, security
from bancaemdia.db.session import get_db_primary
from bancaemdia.models.coleta_instalacao import ColetaInstalacao
from bancaemdia.observability.credentials import COLLECTION_SECRET
from bancaemdia.repositories.coleta_instalacao import ColetaInstalacaoRepo
from bancaemdia.services import coleta_tokens


class PrivateValidationRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def private(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default validation body echoes invalid input, including codes.
                return JSONResponse({"detail": "Invalid pairing request"}, status_code=422)

        return private


router = APIRouter(
    prefix="/api/v1/coleta",
    route_class=PrivateValidationRoute,
    responses={
        **AUTHENTICATED_ERROR_RESPONSES,
        **COLETA_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Installation unavailable"},
    },
)


class PairingExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    codigo: SecretStr = Field(min_length=1, max_length=128)
    instalacao_publica_id: UUID
    nome_dispositivo: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("nome_dispositivo")
    @classmethod
    def label_is_not_a_credential(cls, value: str | None) -> str | None:
        if value is not None and (
            COLLECTION_SECRET.search(value) or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("Invalid device label")
        return value


class PairingCodeResponse(BaseModel):
    codigo: str
    expira_em: datetime


class InstallationTokenResponse(BaseModel):
    instalacao_id: int
    token: str


class InstallationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    instalacao_publica_id: UUID
    nome_dispositivo: str | None
    token_prefixo: str | None
    criado_em: datetime
    pareado_em: datetime | None
    ultimo_uso_em: datetime | None
    rotacionado_em: datetime | None
    revogado_em: datetime | None
    expira_em: datetime | None


class InstallationStatusResponse(BaseModel):
    usuario_id: int
    instalacao_id: int


async def owner(
    request: Request,
    session: AsyncSession = Depends(get_db_primary),
    _: object = Security(security),
) -> int:
    return (await _current_user(request, session)).id


@router.post("/pairing-codes", response_model=PairingCodeResponse, status_code=201)
async def issue_code(
    usuario_id: int = Depends(owner), session: AsyncSession = Depends(get_db_primary)
) -> PairingCodeResponse:
    code, expiry = await coleta_tokens.create_code(session, usuario_id)
    return PairingCodeResponse(codigo=code, expira_em=expiry)


@router.post("/pairing-exchange", response_model=InstallationTokenResponse)
async def exchange_code(
    body: PairingExchange, request: Request, session: AsyncSession = Depends(get_db_primary)
) -> InstallationTokenResponse:
    installation, token = await coleta_tokens.exchange(
        session,
        code=body.codigo.get_secret_value(),
        public_id=body.instalacao_publica_id,
        label=body.nome_dispositivo,
        identity=request.client.host if request.client else "unknown",
    )
    return InstallationTokenResponse(instalacao_id=installation, token=token)


@router.get("/installations", response_model=list[InstallationResponse])
async def list_installations(
    usuario_id: int = Depends(owner), session: AsyncSession = Depends(get_db_primary)
) -> list[InstallationResponse]:
    rows = await session.scalars(
        select(ColetaInstalacao)
        .where(ColetaInstalacao.usuario_id == usuario_id)
        .order_by(ColetaInstalacao.id)
    )
    return [InstallationResponse.model_validate(row) for row in rows]


@router.post("/installations/{instalacao_id}/rotate", response_model=InstallationTokenResponse)
async def rotate(
    instalacao_id: int,
    usuario_id: int = Depends(owner),
    session: AsyncSession = Depends(get_db_primary),
) -> InstallationTokenResponse:
    token = await coleta_tokens.rotate_or_revoke(session, usuario_id, instalacao_id, revoke=False)
    assert token is not None
    return InstallationTokenResponse(instalacao_id=instalacao_id, token=token)


@router.delete("/installations/{instalacao_id}", status_code=204)
async def revoke(
    instalacao_id: int,
    usuario_id: int = Depends(owner),
    session: AsyncSession = Depends(get_db_primary),
) -> Response:
    await coleta_tokens.rotate_or_revoke(session, usuario_id, instalacao_id, revoke=True)
    return Response(status_code=204)


@router.get("/status", response_model=InstallationStatusResponse)
async def installation_status(
    request: Request, session: AsyncSession = Depends(get_db_primary)
) -> InstallationStatusResponse:
    token = request.headers.get("X-Coleta-Token", "")
    identity = (
        await ColetaInstalacaoRepo().authenticate(session, coleta_tokens.digest(token))
        if token and len(token) <= 128
        else None
    )
    if identity is None:
        coleta_tokens.audit("auth_rejected")
        raise HTTPException(403, "Invalid collection credential")
    result = InstallationStatusResponse(
        usuario_id=identity.usuario_id, instalacao_id=identity.instalacao_id
    )
    await session.commit()
    return result
