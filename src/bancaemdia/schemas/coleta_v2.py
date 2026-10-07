"""The API owns the transport contract; client hints never define financial facts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ObservedTransport(StrictModel):
    source: Literal["observed_response"]
    transport: Literal["fetch", "xhr"]
    method: Literal["GET", "POST"]
    path: str = Field(min_length=1, max_length=256, pattern=r"^/[^?#\\\s]*$")
    status: Literal[200]
    content_type: Literal["application/json"]
    adapter_version: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    sanitization_version: Literal[1]


class Capture(StrictModel):
    client_event_id: UUID
    hostname: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
    observado: ObservedTransport
    capturado_em: AwareDatetime
    payload: dict[str, JsonValue]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    conta_casa_ref: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)

    @field_validator("payload")
    @classmethod
    def encodable_json(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        from bancaemdia.domain.coleta_provenance import canonical_bytes

        try:
            canonical_bytes(value)
        except (ValueError, UnicodeEncodeError):
            raise ValueError("Invalid JSON value") from None
        return value

    # Hints are deliberately absent: the parser alone derives identity and lifecycle.


class CollectionBatch(StrictModel):
    contrato: Literal[2]
    batch_id: UUID
    sessao_id: UUID
    items: list[Capture] = Field(min_length=1, max_length=100)


class SessionRequest(StrictModel):
    coletar_desde: AwareDatetime
    retomar_sessao_id: UUID | None = None


class SessionResponse(StrictModel):
    sessao_id: UUID
    coletar_desde: datetime
    encerrada_em: datetime | None


class SubmissionAck(StrictModel):
    client_event_id: UUID
    content_hash: str
    ack: Literal["accepted", "duplicate", "rejected"]
    reason: str
    retryable: bool
    job_id: UUID | None


class BatchAck(StrictModel):
    contrato: Literal[2] = 2
    batch_id: UUID
    items: list[SubmissionAck]


class JobStatus(StrictModel):
    job_id: UUID
    client_event_id: UUID
    status: Literal[
        "pending",
        "materialized",
        "updated",
        "ignored_before_boundary",
        "needs_review",
        "failed",
        "duplicate",
    ]
    reason: str
    aposta_chave: str | None
