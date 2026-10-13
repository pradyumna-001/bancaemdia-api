"""Site result workflow: extra inputs are refused, financial decisions stay on the server."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Money = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Identity = Annotated[int, Field(ge=1, le=2**63 - 1)]
State = Literal["PENDENTE", "GREEN", "RED", "ANULADA", "CASHOUT", "MEIO_GREEN", "MEIO_RED"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueueFilter(Strict):
    cursor: str | None = Field(default=None, max_length=4096)
    page_size: int = Field(default=50, ge=1, le=100)
    desde: datetime | None = None
    ate: datetime | None = None
    casa_id: Identity | None = None
    titular_id: Identity | None = None
    conta_casa_id: Identity | None = None
    banca_id: Identity | None = None
    tipster_id: Identity | None = None
    mercado_id: Identity | None = None
    competicao_id: Identity | None = None
    time_casa_id: Identity | None = None
    time_fora_id: Identity | None = None
    origem: Literal["manual", "planilha", "print", "casa", "telegram", "telegram_bot"] | None = None
    freebet: bool | None = None

    @model_validator(mode="after")
    def interval(self) -> "QueueFilter":
        for value in (self.desde, self.ate):
            if value is not None and value.tzinfo is None:
                raise ValueError("queue dates require an explicit timezone")
        if self.desde is not None and self.ate is not None and self.desde >= self.ate:
            raise ValueError("desde must precede ate")
        return self


class ResultInput(Strict):
    chave: str = Field(min_length=1, max_length=512)
    revisao_esperada: UUID
    estado: State
    retorno_centavos: Money | None = None
    cashout_valor_centavos: Money | None = None
    comissao_centavos: Money | None = None

    @model_validator(mode="after")
    def monetary_intent(self) -> "ResultInput":
        if self.estado == "PENDENTE" and any(
            value is not None for value in (self.retorno_centavos, self.cashout_valor_centavos)
        ):
            raise ValueError("PENDENTE cannot carry a paid return")
        if self.estado == "CASHOUT" and (
            self.retorno_centavos is not None
            and self.cashout_valor_centavos is not None
            and self.retorno_centavos != self.cashout_valor_centavos
        ):
            raise ValueError("cashout fields disagree")
        return self


class UndoInput(Strict):
    operacao_id: UUID
    revisao_esperada: UUID


class ResultBet(Strict):
    chave: str
    revisao: UUID
    estado: State
    elegivel: bool
    motivo_inelegivel: str | None
    antiguidade: datetime
    criterio_antiguidade: Literal["data_jogo", "criada_em"]
    origem: str
    casa: str | None
    evento: str | None
    descricao: str | None
    mercado: str | None
    data_jogo: datetime | None
    data_aposta: datetime | None
    odd: float | None
    stake_unidades: float
    stake_centavos: int
    valor_aposta_centavos: int
    retorno_centavos: int | None
    retorno_informado: bool | None
    comissao_centavos: int | None
    lucro_centavos: int | None
    freebet: bool
    conta_atribuicao: Literal["ASSIGNED", "UNASSIGNED"]
    conta_contexto: dict[str, JsonValue] | None
    banca_contexto: dict[str, JsonValue] | None
    tipster_id: int | None
    mercado_id: int | None
    competicao_id: int | None


class QueuePage(Strict):
    data: list[ResultBet]
    next_cursor: str | None
    total_elegivel: int
    observado_em: datetime
    fronteira_iniciada_em: datetime
    frescor: Literal["primary_snapshot"]
    populacao: Literal["pending_selected_financial_assigned_without_severe_review"]


class ResultReceipt(Strict):
    id: UUID
    tipo: Literal["result", "undo"]
    aposta: ResultBet
    operacao_desfeita: UUID | None
    confirmado_em: datetime
    desfazer_ate: datetime | None
    consultar_ate: datetime
    eventos_gravados: Literal[1]
