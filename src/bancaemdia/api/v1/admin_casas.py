"""Audited operator reads and tenant-owned manual access confirmations."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, JsonValue, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse
from bancaemdia.coleta.catalogo import Observation, StrictModel
from bancaemdia.db.session import get_db_primary
from bancaemdia.models.casa_dominio import CatalogoConfirmacao
from bancaemdia.models.usuario import Usuario
from bancaemdia.services.catalogo import admin_export, audit

router = APIRouter(
    responses={
        405: {"model": ErrorResponse, "description": "Method not allowed"},
        **AUTHENTICATED_ERROR_RESPONSES,
        400: {"model": ErrorResponse, "description": "Exact host/access confirmation required."},
        403: {
            "model": ErrorResponse,
            "description": "Explicit catalog operator authorization required.",
        },
    }
)


class ManualCandidate(StrictModel):
    brand: str = Field(min_length=1, max_length=120)
    hostname: str = Field(min_length=3, max_length=253)
    access_confirmed: bool
    confirmed_at: datetime
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("confirmed_at")
    @classmethod
    def validate_date(cls, value: datetime) -> datetime:
        if value.utcoffset() is None or value > datetime.now(UTC):
            raise ValueError("confirmation must be timezone-aware and not in the future")
        return value


class ManualReceipt(StrictModel):
    situation: str = "acesso_confirmado"
    brand: str
    hostname: str


@router.post(
    "/api/v1/catalogo/candidatos",
    response_model=ManualReceipt,
    responses={
        400: {"model": ErrorResponse, "description": "Exact host/access confirmation required."}
    },
)
async def candidate(
    body: ManualCandidate, request: Request, session: AsyncSession = Depends(get_db_primary)
) -> ManualReceipt:
    if not body.access_confirmed:
        raise HTTPException(400, "Access must be explicitly confirmed")
    try:
        observation = Observation(
            brand=body.brand, hostname=body.hostname, situation="acesso_confirmado"
        )
    except ValueError as error:
        raise HTTPException(400, "An exact brand and hostname are required") from error
    uid = request.state.usuario_id
    if not await session.scalar(select(Usuario.ativo).where(Usuario.id == uid)):
        raise HTTPException(403, "Active user required")
    inserted = await session.scalar(
        insert(CatalogoConfirmacao)
        .values(
            usuario_id=uid,
            marca=observation.brand,
            hostname=observation.hostname,
            confirmado_em=body.confirmed_at,
            evidence_sha256=body.evidence_sha256,
        )
        .on_conflict_do_nothing()
        .returning(CatalogoConfirmacao.id)
    )
    if inserted is not None:
        audit(session, uid, "manual_confirmed", {"confirmation_id": inserted})
    await session.commit()
    return ManualReceipt(brand=observation.brand, hostname=observation.hostname)


@router.get(
    "/api/v1/admin/casas",
    response_model=dict[str, JsonValue],
    responses={
        403: {
            "model": ErrorResponse,
            "description": "Explicit catalog operator authorization required.",
        }
    },
)
@router.get(
    "/api/v1/admin/casas/export",
    response_model=dict[str, JsonValue],
    responses={
        403: {
            "model": ErrorResponse,
            "description": "Explicit catalog operator authorization required.",
        }
    },
)
async def coverage(
    request: Request, session: AsyncSession = Depends(get_db_primary)
) -> dict[str, JsonValue]:
    result = await admin_export(session, request.state.usuario_id)
    await session.commit()
    return result
