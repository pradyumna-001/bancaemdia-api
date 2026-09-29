from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from bancaemdia.api.collection_contract import CollectionRelease, artifact, release, serialized
from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    COLETA_ERROR_RESPONSES,
    ErrorResponse,
)
from bancaemdia.api.v1.coleta_pairing import PrivateValidationRoute
from bancaemdia.db.session import get_db_primary
from bancaemdia.models.coleta_sessao import ColetaEntrega, ColetaSessao
from bancaemdia.repositories.coleta_instalacao import ColetaInstalacaoRepo, CollectionIdentity
from bancaemdia.schemas.coleta_v2 import (
    BatchAck,
    CollectionBatch,
    JobStatus,
    SessionRequest,
    SessionResponse,
)
from bancaemdia.services.coleta_ingest import notify_worker, session_for, submit
from bancaemdia.services.coleta_tokens import audit, digest

router = APIRouter(
    prefix="/api/v1/coleta",
    route_class=PrivateValidationRoute,
    responses={
        **AUTHENTICATED_ERROR_RESPONSES,
        **COLETA_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Collection resource unavailable"},
        409: {"model": ErrorResponse, "description": "Explicit session selection required"},
    },
)


async def installation(
    request: Request, session: AsyncSession = Depends(get_db_primary)
) -> CollectionIdentity:
    token = request.headers.get("X-Coleta-Token", "")
    identity = (
        await ColetaInstalacaoRepo().authenticate(session, digest(token))
        if token and len(token) <= 128
        else None
    )
    if identity is None:
        audit("auth_rejected")
        raise HTTPException(
            401, "Invalid collection credential", headers={"WWW-Authenticate": "Collection"}
        )
    request.state.usuario_id = identity.usuario_id
    request.state.instalacao_id = identity.instalacao_id
    return identity


def describe(row: ColetaSessao) -> SessionResponse:
    return SessionResponse(
        sessao_id=row.sessao_id, coletar_desde=row.coletar_desde, encerrada_em=row.encerrada_em
    )


@router.post("/sessions", response_model=SessionResponse)
async def open_session(
    body: SessionRequest,
    identity: CollectionIdentity = Depends(installation),
    session: AsyncSession = Depends(get_db_primary),
) -> SessionResponse:
    if body.retomar_sessao_id is not None:
        row = await session_for(session, identity, body.retomar_sessao_id)
        if row.encerrada_em is not None or row.coletar_desde != body.coletar_desde:
            raise HTTPException(409, "Session boundary cannot change")
    else:
        if body.coletar_desde > datetime.now(UTC) + timedelta(minutes=5):
            raise HTTPException(422, "Invalid session boundary")
        existing = await session.scalar(
            select(ColetaSessao.id).where(
                ColetaSessao.instalacao_id == identity.instalacao_id,
                ColetaSessao.encerrada_em.is_(None),
            )
        )
        if existing is not None:
            raise HTTPException(409, "Resume or close the explicit open session")
        row = ColetaSessao(
            usuario_id=identity.usuario_id,
            instalacao_id=identity.instalacao_id,
            coletar_desde=body.coletar_desde,
        )
        session.add(row)
        await session.flush()
    response = describe(row)
    await session.commit()
    return response


@router.get("/sessions/{sessao_id}", response_model=SessionResponse)
async def read_session(
    sessao_id: UUID,
    identity: CollectionIdentity = Depends(installation),
    session: AsyncSession = Depends(get_db_primary),
) -> SessionResponse:
    response = describe(await session_for(session, identity, sessao_id))
    await session.commit()
    return response


@router.delete("/sessions/{sessao_id}", status_code=204)
async def close_session(
    sessao_id: UUID,
    identity: CollectionIdentity = Depends(installation),
    session: AsyncSession = Depends(get_db_primary),
) -> Response:
    row = await session_for(session, identity, sessao_id)
    row.encerrada_em = row.encerrada_em or datetime.now(UTC)
    await session.commit()
    return Response(status_code=204)


@router.post("/batches", response_model=BatchAck)
async def receive_batch(
    body: CollectionBatch,
    identity: CollectionIdentity = Depends(installation),
    session: AsyncSession = Depends(get_db_primary),
) -> BatchAck:
    response = await submit(session, identity, body)
    await run_in_threadpool(notify_worker)
    return response


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def job_status(
    job_id: UUID,
    identity: CollectionIdentity = Depends(installation),
    session: AsyncSession = Depends(get_db_primary),
) -> JobStatus:
    row = await session.scalar(
        select(ColetaEntrega).where(
            ColetaEntrega.job_id == job_id,
            ColetaEntrega.usuario_id == identity.usuario_id,
            ColetaEntrega.instalacao_id == identity.instalacao_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Collection job unavailable")
    response = JobStatus.model_validate({
        "job_id": row.job_id,
        "client_event_id": row.client_event_id,
        "status": row.status,
        "reason": row.reason,
        "aposta_chave": row.aposta_chave,
    })
    await session.commit()
    return response


@router.get("/contract", response_model=CollectionRelease)
def published_release() -> CollectionRelease:
    return release()


@router.get("/contract/schema", response_model=dict[str, Any])
def published_schema() -> Response:
    return Response(serialized(artifact()), media_type="application/json")
