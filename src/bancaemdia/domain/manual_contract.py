"""Public synchronous manual-creation protocol; no client financial calculations."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator

from bancaemdia.domain.materializar import casa_canonica

ExactId = Annotated[str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,18}$")]
ExactMoney = Annotated[str, StringConstraints(strict=True, pattern=r"^(0|[1-9][0-9]{0,18})$")]
ManualField = Literal[
    "intencao_id",
    "casa",
    "data_aposta",
    "data_jogo",
    "odd",
    "stake_unidades",
    "evento",
    "descricao",
    "mercado_bruto",
    "freebet",
    "comissao_centavos",
    "conta_casa_id",
]
ManualCode = Literal[
    "manual_validation_invalid",
    "manual_account_invalid",
    "manual_creation_busy",
    "manual_intention_conflict",
    "manual_intention_not_found",
    "manual_receipt_expired",
    "manual_creation_unavailable",
]


class ManualCreationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    casa: str = Field(min_length=1, max_length=200)
    data_aposta: str | None = Field(
        default=None,
        description="Omitida ou null: relógio do servidor somente na primeira criação; replay conserva a confirmação original. Não define a conta temporal.",
    )
    data_jogo: str | None = Field(
        default=None,
        description="Define a conta pelo instante do jogo. Ausente ou ambígua: revisão sem presumir conta pela data da aposta. Multicontas explícita usa a conta informada.",
    )
    odd: float = Field(ge=1.01, le=1000, strict=True)
    stake_unidades: float = Field(gt=0, strict=True)
    evento: str | None = Field(default=None, max_length=10000)
    descricao: str | None = Field(default=None, max_length=10000)
    mercado_bruto: str | None = Field(default=None, max_length=10000)
    freebet: StrictBool = False
    comissao_centavos: ExactMoney | None = None
    conta_casa_id: ExactId | None = None

    @field_validator("casa")
    @classmethod
    def canonical_house(cls, value: str) -> str:
        name = casa_canonica(value)
        if name is None:
            raise ValueError("unknown house")
        return name

    @field_validator("data_aposta", "data_jogo")
    @classmethod
    def canonical_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
        return instant.astimezone(UTC).isoformat()

    @field_validator("conta_casa_id", "comissao_centavos")
    @classmethod
    def bigint(cls, value: str | None) -> str | None:
        if value is not None and int(value) > 2**63 - 1:
            raise ValueError("outside bigint")
        return value


class ManualAssignmentNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    codigo: Literal["conta_ausente", "conta_ambigua"]
    revisao_id: ExactId
    encaminhamento: Literal["revisar_conta"] = "revisar_conta"


class ManualOriginalConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_aposta: datetime
    data_jogo: datetime | None
    conta_casa_id: ExactId | None
    criterio_conta: Literal["data_jogo", "multicontas_explicita"]
    valor_unidade_centavos: ExactMoney
    stake_centavos: ExactMoney
    valor_aposta_centavos: ExactMoney
    comissao_centavos: ExactMoney
    freebet: bool


class ManualCreationReceipt(BaseModel):
    """Original confirmation, never a live financial projection of the bet."""

    model_config = ConfigDict(extra="forbid")
    intencao_id: UUID
    estado: Literal["confirmada"] = "confirmada"
    aposta_chave: str
    detalhe_url: str
    confirmado_em: datetime
    recuperavel_ate: datetime
    confirmacao_original: ManualOriginalConfirmation
    avisos: list[ManualAssignmentNotice]


class ManualCreationError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: ManualCode
    campo: ManualField | None = None
    efeito: Literal[
        "nenhuma_gravacao_neste_pedido", "resultado_desconhecido", "confirmacao_anterior_retida"
    ] = "nenhuma_gravacao_neste_pedido"
