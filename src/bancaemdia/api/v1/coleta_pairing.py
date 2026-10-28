from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
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
from bancaemdia.models.coleta_sessao import ColetaSessao
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


class OwnerCollectionSessionResponse(BaseModel):
    """Original collection boundary; this is not presence or credential validity."""

    model_config = ConfigDict(extra="forbid")
    sessao_id: UUID
    selecao: Literal["atual", "ultima_encerrada"]
    coletar_desde: datetime
    criada_em: datetime
    encerrada_em: datetime | None = Field(
        description="Null para seleção atual; timestamp obrigatório para ultima_encerrada."
    )

    @model_validator(mode="after")
    def consistent_boundary(self) -> "OwnerCollectionSessionResponse":
        if (self.selecao == "atual") != (self.encerrada_em is None):
            raise ValueError("Inconsistent collection session boundary")
        return self


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
    estado_sessao: Literal["sem_sessao", "aberta", "encerrada"] = Field(
        description="Collection session state; never online presence."
    )
    sessao_coleta: OwnerCollectionSessionResponse | None = Field(
        description="Null only when this installation has no persisted session."
    )


class InstallationStatusResponse(BaseModel):
    usuario_id: int
    instalacao_id: int


async def owner(
    request: Request,
    session: AsyncSession = Depends(get_db_primary),
    _: object = Security(security),
) -> int:
    try:
        return (await _current_user(request, session)).id
    except SQLAlchemyError:
        raise HTTPException(
            503, "Collection management temporarily unavailable", headers={"Retry-After": "5"}
        ) from None


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


@router.get(
    "/installations",
    response_model=list[InstallationResponse],
    responses={
        503: {
            "model": ErrorResponse,
            "description": "Owner authentication/read dependency unavailable; no collection effects.",
            "headers": {"Retry-After": {"schema": {"type": "string"}}},
        },
    },
)
async def list_installations(
    usuario_id: int = Depends(owner), session: AsyncSession = Depends(get_db_primary)
) -> list[InstallationResponse]:
    # One statement/snapshot for the whole owner population, not one query/device.
    # Only public projection columns are loaded; no token hash or capture payload.
    ranked = (
        select(
            ColetaSessao.instalacao_id,
            ColetaSessao.sessao_id,
            ColetaSessao.coletar_desde,
            ColetaSessao.criada_em,
            ColetaSessao.encerrada_em,
            func
            .row_number()
            .over(
                partition_by=ColetaSessao.instalacao_id,
                order_by=(
                    ColetaSessao.encerrada_em.is_(None).desc(),
                    ColetaSessao.criada_em.desc(),
                    ColetaSessao.id.desc(),
                ),
            )
            .label("position"),
        )
        .where(ColetaSessao.usuario_id == usuario_id)
        .subquery()
    )
    fields = (
        "id",
        "instalacao_publica_id",
        "nome_dispositivo",
        "token_prefixo",
        "criado_em",
        "pareado_em",
        "ultimo_uso_em",
        "rotacionado_em",
        "revogado_em",
        "expira_em",
    )
    query = (
        select(
            *(getattr(ColetaInstalacao, field) for field in fields),
            ranked.c.sessao_id,
            ranked.c.coletar_desde,
            ranked.c.criada_em,
            ranked.c.encerrada_em,
        )
        .outerjoin(
            ranked,
            and_(ranked.c.instalacao_id == ColetaInstalacao.id, ranked.c.position == 1),
        )
        .where(ColetaInstalacao.usuario_id == usuario_id)
        .order_by(ColetaInstalacao.id)
    )
    try:
        rows = (await session.execute(query)).mappings().all()
    except SQLAlchemyError:
        raise HTTPException(
            503, "Collection management temporarily unavailable", headers={"Retry-After": "5"}
        ) from None
    result = []
    for row in rows:
        projection = None
        state: Literal["sem_sessao", "aberta", "encerrada"] = "sem_sessao"
        if row["sessao_id"] is not None:
            state = "aberta" if row["encerrada_em"] is None else "encerrada"
            projection = OwnerCollectionSessionResponse(
                sessao_id=row["sessao_id"],
                selecao="atual" if state == "aberta" else "ultima_encerrada",
                coletar_desde=row["coletar_desde"],
                criada_em=row["criada_em"],
                encerrada_em=row["encerrada_em"],
            )
        result.append(
            InstallationResponse(
                **{field: row[field] for field in fields},
                estado_sessao=state,
                sessao_coleta=projection,
            )
        )
    return result


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
