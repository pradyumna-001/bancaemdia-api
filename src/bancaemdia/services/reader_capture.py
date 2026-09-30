"""Parse atomically or quarantine. This boundary never creates financial projections."""

from dataclasses import dataclass

import structlog
from prometheus_client import Counter
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.coleta.readers.base import (
    ParsedCapture,
    ReaderEnvelope,
    canonical_bytes,
    decode_raw,
    digest,
)
from bancaemdia.coleta.readers.errors import ReaderError
from bancaemdia.coleta.readers.registry import ReaderRegistry
from bancaemdia.coleta.readers.safety import check_safe
from bancaemdia.models.reader_quarantine import ReaderQuarantine
from bancaemdia.models.usuario import Usuario

reader_outcomes = Counter(
    "reader_capture_outcomes_total",
    "Backend reader outcomes without capture labels",
    ["outcome", "error_code"],
)


@dataclass(frozen=True)
class ReaderOutcome:
    parsed: ParsedCapture | None
    quarantine_id: int | None
    error_code: str | None
    reason: str | None


def safe_evidence(value: object) -> dict[str, object] | None:
    try:
        envelope = ReaderEnvelope.model_validate(value)
        check_safe(envelope.model_dump(mode="json", exclude={"payload_text"}))
        if envelope.content_type != "application/json":
            return None
        decode_raw(envelope.payload_text)
        return envelope.model_dump(mode="json")
    except (ValueError, TypeError, RecursionError):
        return None


async def parse_or_quarantine(
    session: AsyncSession, usuario_id: int, value: object, registry: ReaderRegistry
) -> ReaderOutcome:
    # Caller must already have the authenticated tenant scope. Never choose a tenant from capture data.
    scoped = await session.scalar(
        text("SELECT NULLIF(current_setting('app.current_user_id', true), '')::bigint")
    )
    if scoped != usuario_id or not await session.scalar(
        select(Usuario.ativo).where(Usuario.id == usuario_id)
    ):
        raise PermissionError("authenticated active tenant scope required")
    try:
        parsed = registry.read(value)
    except ReaderError as error:
        try:
            # Digest is irreversible; unsafe bodies and parser exception text are omitted.
            raw = canonical_bytes(
                value.model_dump(mode="json") if isinstance(value, ReaderEnvelope) else value
            )
        except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
            raw = b"unencodable-reader-envelope"
        fingerprint = digest(raw)
        statement = (
            insert(ReaderQuarantine)
            .values(
                usuario_id=usuario_id,
                capture_sha256=fingerprint,
                error_code=error.code.value,
                reason=error.reason,
                envelope=None if error.code.value == "unsafe_payload" else safe_evidence(value),
            )
            .on_conflict_do_nothing(constraint="uq_reader_quarantine_capture")
            .returning(ReaderQuarantine.id)
        )
        row_id = await session.scalar(statement)
        if row_id is None:
            row_id = await session.scalar(
                select(ReaderQuarantine.id).where(
                    ReaderQuarantine.usuario_id == usuario_id,
                    ReaderQuarantine.capture_sha256 == fingerprint,
                    ReaderQuarantine.error_code == error.code.value,
                    ReaderQuarantine.reason == error.reason,
                )
            )
        reader_outcomes.labels(outcome="quarantined", error_code=error.code.value).inc()
        structlog.get_logger(__name__).warning(
            "reader_capture_quarantined", error_code=error.code.value, reason=error.reason
        )
        return ReaderOutcome(None, row_id, error.code.value, error.reason)
    reader_outcomes.labels(outcome="parsed", error_code="none").inc()
    return ReaderOutcome(parsed, None, None, None)
