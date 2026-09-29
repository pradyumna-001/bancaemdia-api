from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictContractModel(BaseModel):
    """Base for public API contracts: undeclared fields never become part of the contract."""

    model_config = ConfigDict(extra="forbid")


class ErrorResponse(StrictContractModel):
    """Structured errors emitted by application and cross-cutting middleware."""

    detail: str | None = None
    erro: str | None = None
    error: Literal["rate_limited"] | None = None
    retry_after: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def at_least_one_message(self) -> "ErrorResponse":
        if self.detail is None and self.erro is None and self.error is None:
            raise ValueError("an error response needs a message")
        return self


class ValidationIssueResponse(StrictContractModel):
    loc: list[str | int]
    msg: str
    type: str
    input: JsonValue = None
    ctx: dict[str, JsonValue] | None = None


class ValidationErrorResponse(StrictContractModel):
    detail: list[ValidationIssueResponse]


type ErrorOrValidationResponse = ErrorResponse | ValidationErrorResponse


type OpenAPIResponses = dict[int | str, dict[str, Any]]


COMMON_ERROR_RESPONSES: OpenAPIResponses = {
    500: {
        "model": ErrorResponse,
        "description": "Unexpected internal failure with no sensitive exception details.",
    }
}

AUTHENTICATED_ERROR_RESPONSES: OpenAPIResponses = {
    401: {
        "model": ErrorResponse,
        "description": "Missing, expired, or invalid bearer token.",
    },
    429: {
        "model": ErrorResponse,
        "description": "The caller exceeded an applicable request limit.",
    },
    422: {
        "model": ErrorOrValidationResponse,
        "description": "The request does not satisfy the published contract or a domain rule.",
    },
    500: COMMON_ERROR_RESPONSES[500],
    503: {
        "model": ErrorResponse,
        "description": "A required dependency is temporarily unavailable.",
    },
}

COLETA_ERROR_RESPONSES: OpenAPIResponses = {
    400: {"model": ErrorResponse, "description": "Malformed collection payload."},
    403: {"model": ErrorResponse, "description": "Invalid collection token."},
    429: {
        "model": ErrorResponse,
        "description": "The collection rate or daily limit was exceeded.",
    },
    500: COMMON_ERROR_RESPONSES[500],
    503: {
        "model": ErrorResponse,
        "description": "The collection was saved but could not be queued.",
    },
}


class PaginationResponse(StrictContractModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)


class BetResponse(StrictContractModel):
    chave: str
    origem: str
    estado: str
    odd: float | None
    stake_unidades: float
    stake_centavos: int
    valor_aposta_centavos: int
    retorno_centavos: int | None
    lucro_centavos: int | None
    freebet: bool
    conta_casa_id: int | None
    conta_atribuicao: Literal["ASSIGNED", "UNASSIGNED"] = "UNASSIGNED"
    tipster_id: int | None
    time_casa_id: int | None
    time_fora_id: int | None
    mercado_id: int | None
    competicao_id: int | None
    data_aposta: datetime | None
    data_jogo: datetime | None
    chat_id: int | None
    message_id: int | None
    midia_hash: str | None
    revisao_grave: bool
    apagada: bool
    casa: str | None
    evento: str | None
    descricao: str | None
    mercado: str | None
    criada_em: datetime | None
    atualizada_em: datetime | None


class BetsPageResponse(StrictContractModel):
    data: list[BetResponse]
    pagination: PaginationResponse


class BetSelectionsResponse(StrictContractModel):
    descricao: str | None
    mercado: str | None
    evento: str | None
    casa: str | None


class BetEventResponse(StrictContractModel):
    tipo: str
    fonte: str
    payload: dict[str, JsonValue]
    confianca: float | None
    criado_em: datetime | None


class PendingReviewResponse(StrictContractModel):
    id: int
    motivo: str
    midia_hash: str | None
    criado_em: datetime | None


class BetDetailResponse(StrictContractModel):
    aposta: BetResponse
    selecoes: BetSelectionsResponse
    eventos: list[BetEventResponse]
    revisao_pendente: PendingReviewResponse | None


class BetChangedResponse(StrictContractModel):
    aposta: BetResponse
    eventos_gravados: int = Field(ge=0)


class BetCreatedResponse(StrictContractModel):
    aposta: BetResponse
    casa_id: int | None
    aviso: str | None = None


class CollectionRejectedItem(StrictContractModel):
    posicao: int = Field(ge=0)
    motivo: str
    identidade: str | None = None


class CollectionWithoutReaderItem(StrictContractModel):
    casa: str
    guardadas: int = Field(ge=0)
    recado: str


class CollectionResponse(StrictContractModel):
    antes_do_inicio: int = Field(ge=0)
    novas_contando: int = Field(ge=0)
    iguais_a_existentes: int = Field(ge=0)
    em_duvida: int = Field(ge=0)
    atualizadas: int = Field(ge=0)
    ja_conhecidas: int = Field(ge=0)
    recusadas: list[CollectionRejectedItem]
    sem_leitor: list[CollectionWithoutReaderItem]


class UploadAcceptedResponse(StrictContractModel):
    job_id: UUID
    estimated_bets: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    status_url: str
    aviso: str | None = None


class UploadProgressResponse(StrictContractModel):
    total: int = Field(ge=0)
    pending: int = Field(ge=0)
    read: int = Field(ge=0)
    failed: int = Field(ge=0)
    ignored: int = Field(ge=0)
    over_limit: int = Field(ge=0)
    percent: int = Field(ge=0, le=100)


class UploadStatusResponse(StrictContractModel):
    job_id: UUID
    status: str
    filename: str
    total_messages: int = Field(ge=0)
    estimated_bets: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    progress: UploadProgressResponse
    bets_processed: int = Field(ge=0)
    bets_failed: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    erro: str | None
    criado_em: datetime
    concluido_em: datetime | None


class LivenessResponse(StrictContractModel):
    status: Literal["ok"]


class ReadinessCheckResponse(StrictContractModel):
    status: Literal["ok", "failed", "degraded"]
    latency_ms: float = Field(ge=0)
    impact: Literal["required", "report_only"]
    details: dict[str, JsonValue] | None = None


class ReadinessResponse(StrictContractModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, ReadinessCheckResponse]
