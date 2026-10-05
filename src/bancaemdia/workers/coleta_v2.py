"""Database inbox polling survives broker loss and worker death between transactions."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import structlog
from prometheus_client import Counter
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bancaemdia import models
from bancaemdia.coleta.leitores import LEITORES
from bancaemdia.coleta.leitura import ColetaInvalidaError
from bancaemdia.domain.access import AccountReadOnlyError, require_write_access
from bancaemdia.domain.account_attribution import ResolutionStatus
from bancaemdia.domain.account_attribution_service import (
    InvalidAccountReferenceError,
    attribute_account,
    game_instant,
)
from bancaemdia.domain.coleta_casa import ApostaInvalidaError, hash_do_conteudo
from bancaemdia.domain.coleta_provenance import HOSTS, canonical_ticket, content_hash, source_times
from bancaemdia.domain.materializar import casa_canonica
from bancaemdia.domain.registros import ColetaCasa
from bancaemdia.models.coleta_sessao import ColetaEntrega, ColetaSessao
from bancaemdia.repositories.base import colunas
from bancaemdia.repositories.coleta_instalacao import owner_scope
from bancaemdia.workers.celery_app import app
from bancaemdia.workers.materialization import _gravar_coletada, get_engine

terminal_total = Counter(
    "coleta_terminal_total", "Terminal collection outcomes", ["contract", "status"]
)


def finish(row: ColetaEntrega, status: str, reason: str) -> None:
    row.status, row.reason = status, reason
    row.finalizada_em = datetime.now(UTC)


async def materialize(session: AsyncSession, row: ColetaEntrega) -> None:
    envelope = row.envelope
    if envelope is None:
        finish(row, "failed", "payload_unavailable")
        return
    raw = envelope["payload"]
    if content_hash(raw) != row.content_hash:
        finish(row, "failed", "stored_hash_mismatch")
        return
    casa = HOSTS[envelope["hostname"]]
    assert casa is not None
    if casa not in LEITORES:
        finish(row, "needs_review", "reader_unavailable")
        return
    occurred, revised = source_times(casa, raw)
    boundary = await session.get(ColetaSessao, row.sessao_id)
    assert boundary is not None
    if occurred is None or revised is None:
        finish(row, "needs_review", "source_time_untrusted")
        return
    if occurred < boundary.coletar_desde:
        finish(row, "ignored_before_boundary", "ticket_before_boundary")
        return
    try:
        parsed = replace(LEITORES[casa](raw), casa=casa)
    except (ColetaInvalidaError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        finish(row, "needs_review", "parser_unrecognized")
        return
    casa_id = await session.scalar(
        select(models.Casa.id).where(models.Casa.nome == casa_canonica(casa))
    )
    reference = envelope.get("conta_casa_ref")
    try:
        resolution = await attribute_account(
            session,
            row.usuario_id,
            casa,
            game_instant({"data_jogo": parsed.comeca_em}),
            reference,
        )
    except InvalidAccountReferenceError:
        finish(row, "needs_review", "account_unavailable")
        return
    if resolution.status != ResolutionStatus.UNIQUE:
        finish(
            row,
            "needs_review",
            "account_reference_required" if reference is None else "account_unavailable",
        )
        return
    account = await session.scalar(
        select(models.ContaCasa)
        .where(models.ContaCasa.id == resolution.conta_casa_id)
        .with_for_update()
    )
    if account is None:
        finish(row, "needs_review", "account_unavailable")
        return
    # Insert before locking: ON CONFLICT serializes first deliveries of the same ticket,
    # including another installation and the v1 path.
    await session.execute(
        insert(models.ColetaCasa)
        .values(
            usuario_id=row.usuario_id,
            casa_id=casa_id,
            identidade=parsed.identidade,
            hash_conteudo=hash_do_conteudo(raw),
            bruto_json=raw,
        )
        .on_conflict_do_nothing(constraint="uq_coletas_casa_identidade")
    )
    current = await session.scalar(
        select(models.ColetaCasa)
        .where(
            models.ColetaCasa.usuario_id == row.usuario_id,
            models.ColetaCasa.casa_id == casa_id,
            models.ColetaCasa.identidade == parsed.identidade,
        )
        .with_for_update()
    )
    assert current is not None
    canonical = canonical_ticket(parsed)
    row.aposta_chave = f"c:{casa}:{parsed.identidade}"
    existing = await session.scalar(
        select(models.Aposta).where(
            models.Aposta.usuario_id == row.usuario_id, models.Aposta.chave == row.aposta_chave
        )
    )
    if existing is not None and existing.conta_casa_id != account.id:
        finish(row, "needs_review", "account_conflict")
        return
    if current.v2_hash == canonical and existing is not None:
        if current.v2_fonte_em is None or revised > current.v2_fonte_em:
            current.v2_fonte_em = revised
        finish(row, "duplicate", "identical_canonical_content")
        return
    if current.v2_fonte_em is not None and revised <= current.v2_fonte_em:
        finish(
            row,
            "needs_review" if revised == current.v2_fonte_em else "duplicate",
            "source_version_conflict" if revised == current.v2_fonte_em else "stale_source",
        )
        return
    if existing is not None and existing.estado != "PENDENTE" and parsed.estado == "PENDENTE":
        finish(row, "needs_review", "reopening_requires_review")
        return
    current.bruto_json = raw
    current.hash_conteudo = hash_do_conteudo(raw)
    current.recebido_em = datetime.now(UTC)
    await session.flush()
    result = await _gravar_coletada(
        session,
        row.usuario_id,
        [ColetaCasa(**colunas(current))],
        casa,
        str(casa_canonica(casa)),
        conta_casa_id=account.id,
        explicit_account=reference is not None,
    )
    current.v2_fonte_em, current.v2_hash = revised, canonical
    current.processado_em = datetime.now(UTC)
    finish(
        row,
        "needs_review"
        if result.revisao_grave
        else ("materialized" if result.criada else "updated"),
        "financial_review" if result.revisao_grave else "processed",
    )


async def process_job(engine: AsyncEngine, usuario_id: int, job_id: UUID) -> str | None:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        await owner_scope(session, usuario_id)
        row = await session.scalar(
            select(ColetaEntrega)
            .where(ColetaEntrega.job_id == job_id, ColetaEntrega.usuario_id == usuario_id)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        if row.status != "pending":
            return row.status
        row.tentativas += 1
        try:
            async with session.begin_nested():
                await require_write_access(session, usuario_id)
                await materialize(session, row)
        except SQLAlchemyError:
            # The entire transaction rolls back; a future poll retries the persisted inbox.
            raise
        except ApostaInvalidaError:
            await session.refresh(row)
            finish(row, "needs_review", "invalid_financial_values")
        except AccountReadOnlyError:
            await session.refresh(row)
            row.reason = "account_read_only"
            row.tentativas -= 1
        except Exception as error:
            await session.refresh(row)
            structlog.get_logger(__name__).warning(
                "collection_processing_failed", error_type=type(error).__name__
            )
            if row.tentativas >= 3:
                finish(row, "failed", "processing_failed")
        result = row.status
    if result != "pending":
        terminal_total.labels(contract="2", status=result).inc()
    return result


async def drain(engine: AsyncEngine, limit: int = 100) -> int:
    async with AsyncSession(engine) as session:
        pending = (
            await session.execute(
                text("SELECT * FROM coleta_pending_deliveries(:maximum)"), {"maximum": limit}
            )
        ).all()
    for user, job in pending:
        await process_job(engine, user, job)
    return len(pending)


def drain_task() -> dict[str, object]:
    try:
        return {"processed": asyncio.run(drain(get_engine()))}
    except SQLAlchemyError:
        structlog.get_logger(__name__).warning("collection_database_unavailable")
        return {"processed": 0, "status": "retry_pending"}


drain_registered = app.task(name="materialization.collection_v2")(drain_task)
