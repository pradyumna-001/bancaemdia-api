"""Atomic submission ACKs and a durable, restartable collection inbox."""

from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import HTTPException
from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.coleta_provenance import (
    HOSTS,
    MAX_ITEM_BYTES,
    canonical_bytes,
    content_hash,
    safe_payload,
)
from bancaemdia.models.coleta_sessao import ColetaEntrega, ColetaSessao
from bancaemdia.repositories.coleta_instalacao import CollectionIdentity
from bancaemdia.schemas.coleta_v2 import BatchAck, CollectionBatch, SubmissionAck
from bancaemdia.services.coleta_tokens import digest

submission_total = Counter(
    "coleta_submission_total", "Durable item acknowledgements", ["contract", "result"]
)


def ack_for(row: ColetaEntrega) -> SubmissionAck:
    return SubmissionAck.model_validate({
        "client_event_id": row.client_event_id,
        "content_hash": row.content_hash,
        "ack": row.ack,
        "reason": row.ack_reason,
        "retryable": False,
        "job_id": None if row.ack == "rejected" else row.job_id,
    })


async def session_for(
    session: AsyncSession, identity: CollectionIdentity, public: UUID
) -> ColetaSessao:
    row = await session.scalar(
        select(ColetaSessao)
        .where(
            ColetaSessao.sessao_id == public,
            ColetaSessao.usuario_id == identity.usuario_id,
            ColetaSessao.instalacao_id == identity.instalacao_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Collection session unavailable")
    return row


async def submit(
    session: AsyncSession, identity: CollectionIdentity, batch: CollectionBatch
) -> BatchAck:
    boundary = await session_for(session, identity, batch.sessao_id)
    acks = []
    for item in batch.items:
        envelope = item.model_dump(mode="json")
        fingerprint = digest(
            str(batch.sessao_id) + canonical_bytes(envelope).decode(), "collection-request"
        )
        previous = await session.scalar(
            select(ColetaEntrega).where(
                ColetaEntrega.instalacao_id == identity.instalacao_id,
                ColetaEntrega.client_event_id == item.client_event_id,
            )
        )
        if previous is not None:
            if previous.request_hash == fingerprint:
                acks.append(ack_for(previous))
            else:
                acks.append(
                    SubmissionAck(
                        client_event_id=item.client_event_id,
                        content_hash=item.content_hash,
                        ack="rejected",
                        reason="event_id_conflict",
                        retryable=False,
                        job_id=None,
                    )
                )
            continue
        reason = None
        if boundary.encerrada_em is not None:
            reason = "session_closed"
        elif not HOSTS.get(item.hostname):
            reason = "unsupported_exact_host"
        elif not safe_payload(envelope):
            reason = "unsafe_payload"
        elif len(canonical_bytes(item.payload)) > MAX_ITEM_BYTES:
            reason = "item_too_large"
        elif content_hash(item.payload) != item.content_hash:
            reason = "content_hash_mismatch"
        prior = None
        if reason is None:
            prior = await session.scalar(
                select(ColetaEntrega.id)
                .where(
                    ColetaEntrega.sessao_id == boundary.id,
                    ColetaEntrega.content_hash == item.content_hash,
                    ColetaEntrega.envelope["hostname"].astext == item.hostname,
                    ColetaEntrega.envelope["conta_casa_ref"].astext
                    == (None if item.conta_casa_ref is None else str(item.conta_casa_ref)),
                    ColetaEntrega.ack != "rejected",
                )
                .limit(1)
            )
        ack = "rejected" if reason else ("duplicate" if prior else "accepted")
        row = ColetaEntrega(
            usuario_id=identity.usuario_id,
            instalacao_id=identity.instalacao_id,
            sessao_id=boundary.id,
            batch_id=batch.batch_id,
            client_event_id=item.client_event_id,
            request_hash=fingerprint,
            content_hash=item.content_hash,
            envelope=None if reason else envelope,
            ack=ack,
            ack_reason=reason or ("content_already_received" if prior else "durably_received"),
            status="failed" if reason else "pending",
            reason=reason or "awaiting_processing",
            finalizada_em=datetime.now(UTC) if reason else None,
        )
        session.add(row)
        await session.flush()
        acks.append(ack_for(row))
    await session.commit()
    for response_ack in acks:
        submission_total.labels(contract="2", result=response_ack.ack).inc()
    return BatchAck(batch_id=batch.batch_id, items=acks)


def notify_worker() -> None:
    from bancaemdia.workers.celery_app import app

    try:
        app.send_task("materialization.collection_v2")
    except Exception as error:
        # Beat drains the same durable inbox even if this optional latency hint is lost.
        structlog.get_logger(__name__).warning(
            "collection_dispatch_deferred", error_type=type(error).__name__
        )
