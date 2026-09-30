"""Installation-authenticated immutable technical catalog; no regulatory endorsement."""

import importlib
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import COMMON_ERROR_RESPONSES, ErrorResponse
from bancaemdia.api.v1.coleta import hash_do_token
from bancaemdia.coleta.catalogo import StrictModel, Technical, canonical, version
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db_primary
from bancaemdia.models.casa_dominio import CatalogoPublicacao
from bancaemdia.services.catalogo_assinatura import TrustRoot

router = APIRouter(
    responses={
        405: {"model": ErrorResponse, "description": "Method not allowed"},
        **COMMON_ERROR_RESPONSES,
        **{
            code: {"model": ErrorResponse, "description": text}
            for code, text in {
                403: "Invalid installation credential",
                409: "Environment or catalog version conflict",
                426: "Client version unsupported",
                429: "Installation or IP quota exceeded",
                503: "Signed catalog or installation prerequisite unavailable",
            }.items()
        },
    }
)
PATH = "/api/v1/coleta/catalogo"


class CatalogEntry(Technical):
    brand: str = Field(min_length=1, max_length=120)
    hostname: str = Field(pattern=r"^[a-z0-9.-]+$", max_length=253)


class TrustRoots(StrictModel):
    current: TrustRoot
    next: TrustRoot | None


class LastKnownGood(StrictModel):
    maximum_age_seconds: Literal[86400]
    new_hosts_allowed: Literal[False]
    revoked_hosts_allowed: Literal[False]
    requires_previously_verified_signature: Literal[True]


class CatalogPayload(StrictModel):
    catalog_version: int = Field(ge=1, le=2**53 - 1)
    contract_version: Literal[1]
    environment: str = Field(min_length=1, max_length=40)
    issued_at: datetime
    expires_at: datetime
    minimum_client_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    last_known_good: LastKnownGood
    entries: list[CatalogEntry]
    key_id: str = Field(min_length=1, max_length=80)
    algorithm: Literal["Ed25519"]
    trust_roots: TrustRoots


class CatalogEnvelope(StrictModel):
    payload: CatalogPayload
    signature: str


class InstallationIdentity(Protocol):
    usuario_id: int
    instalacao_id: int


class InstallationRepository(Protocol):
    async def authenticate(
        self, session: AsyncSession, token_hash: str
    ) -> InstallationIdentity | None: ...


def installation_repository() -> InstallationRepository:
    # #107 owns pairing, rotation and revocation. Never substitute legacy user tokens.
    try:
        module = importlib.import_module("bancaemdia.repositories.coleta_instalacao")
    except ModuleNotFoundError as error:
        if error.name != "bancaemdia.repositories.coleta_instalacao":
            raise
        raise HTTPException(
            503, "Installation authentication prerequisite #107 is not integrated"
        ) from error
    return cast(InstallationRepository, module.ColetaInstalacaoRepo())


@router.get(
    PATH,
    response_model=CatalogEnvelope,
    responses={
        304: {"description": "Authenticated current catalog is unchanged."},
        403: {
            "model": ErrorResponse,
            "description": "Installation credential is invalid, expired or revoked.",
        },
        409: {
            "model": ErrorResponse,
            "description": "Catalog environment or monotonic version differs.",
        },
        426: {"model": ErrorResponse, "description": "Client version is unsupported."},
        503: {
            "model": ErrorResponse,
            "description": "No unexpired signed publication or installation prerequisite.",
        },
    },
)
async def catalog(
    request: Request,
    client_version: str = Query(pattern=r"^\d+\.\d+\.\d+$"),
    environment: str = Query(min_length=1, max_length=40),
    known_version: int = Query(default=0, ge=0, le=2**53 - 1),
    installation_token: str | None = Header(default=None, alias="X-Coleta-Token", max_length=128),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    session: AsyncSession = Depends(get_db_primary),
    repository: InstallationRepository = Depends(installation_repository),
) -> Response:
    identity = (
        await repository.authenticate(session, hash_do_token(installation_token))
        if installation_token
        else None
    )
    if identity is None:
        raise HTTPException(403, "Invalid installation credential")
    request.state.usuario_id = identity.usuario_id
    request.state.instalacao_id = identity.instalacao_id
    configured = get_settings().APP_ENV
    if environment != configured:
        raise HTTPException(409, "Catalog environment mismatch")
    row = await session.scalar(
        select(CatalogoPublicacao)
        .where(CatalogoPublicacao.ambiente == configured)
        .order_by(CatalogoPublicacao.id.desc())
        .limit(1)
    )
    if row is None or row.expira_em <= datetime.now(UTC):
        raise HTTPException(503, "Signed catalog expired or unavailable")
    if known_version > row.id:
        raise HTTPException(409, "Catalog downgrade refused")
    try:
        parsed_client = version(client_version)
    except ValueError as error:
        raise HTTPException(426, "Stable client version required") from error
    if parsed_client < version(row.envelope["payload"]["minimum_client_version"]):
        raise HTTPException(426, "Client update required")
    # Entry floors are signed; the client can still retrieve tombstones/disable unsupported adapters.
    ttl = max(0, min(300, int((row.expira_em - datetime.now(UTC)).total_seconds())))
    headers = {
        "ETag": f'"{row.etag}"',
        "Cache-Control": f"private, max-age={ttl}, must-revalidate",
        "Vary": "X-Coleta-Token",
        "X-Content-Type-Options": "nosniff",
    }
    await session.commit()  # Auth credential lock/use metadata from #107 commits before response.
    if if_none_match == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    return Response(canonical(row.envelope), media_type="application/json", headers=headers)
