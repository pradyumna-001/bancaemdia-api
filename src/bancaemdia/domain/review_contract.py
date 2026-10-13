"""Public review choices; internal extraction payloads are not part of this contract."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bancaemdia.api.v1.revisao import CorrecaoDaRevisao

Family = Literal[
    "leitura", "conta", "par_legado", "consolidacao", "print_incompleto", "nao_suportada"
]
Action = Literal["CORRIGIR", "DESCARTAR", "MESMA", "DISTINTAS", "RECUSAR_VINCULO"]
Identifier = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")]


class ReviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    familia: Family | None = None
    desde: datetime | None = Field(
        default=None,
        description="Abertura criado_em >= desde; data sem offset é America/Sao_Paulo. Use um intervalo de abertura para recortar idade.",
    )
    ate: datetime | None = Field(
        default=None,
        description="Abertura criado_em < ate; data sem offset é America/Sao_Paulo. Não consulta data_aposta/data_jogo.",
    )
    page: int = Field(default=1, ge=1, le=(2**63 - 1) // 100)
    page_size: int = Field(default=50, ge=1, le=100)

    @field_validator("desde", "ate")
    @classmethod
    def brazil(cls, value: datetime | None) -> datetime | None:
        return (
            value.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
            if value is not None and value.tzinfo is None
            else value
        )

    @model_validator(mode="after")
    def interval(self) -> "ReviewQuery":
        if self.desde is not None and self.ate is not None and self.desde >= self.ate:
            raise ValueError("review_interval_invalid")
        return self


class SourceEvidence(BaseModel):
    chave: str
    papel: Literal["aposta", "casa", "telegram"]
    origem: str
    casa: str | None
    evento: str | None
    descricao: str | None
    mercado: str | None
    data_jogo: datetime | None
    data_aposta: datetime | None
    tipster: str | None
    tipster_id: Identifier | None
    odd: float | None
    stake_unidades: float
    stake_centavos: str
    valor_aposta_centavos: str
    retorno_centavos: str | None
    freebet: bool
    selecionada: bool


class HolderChoice(BaseModel):
    id: Identifier
    nome: str
    arquivado: bool


class AccountChoice(BaseModel):
    id: Identifier
    apelido: str
    estado: str
    ativa: bool
    titular: HolderChoice | None
    elegivel_escolha_explicita: bool
    coincide_no_dia_do_jogo: bool


class AccountEvidence(BaseModel):
    casa: str | None
    casa_id: Identifier | None
    instante: datetime | None
    criterio: Literal["data_jogo", "multicontas_explicita"]
    status: Literal["NONE", "UNIQUE", "AMBIGUOUS", "INVALID"]
    status_na_abertura: Literal["NONE", "UNIQUE", "AMBIGUOUS"] | None = None
    conta_atual_id: Identifier | None
    candidatos: list[AccountChoice]
    encaminhamento: Literal["escolher_conta", "cadastrar_conta_na_casa", "informar_casa"]


class TipsterChoice(BaseModel):
    fonte: Literal["casa", "telegram"]
    rotulo: str
    tipster: str | None
    tipster_id: Identifier | None


class PairEvidence(BaseModel):
    tipo: Literal["legado", "casa_financeira_telegram_contextual"]
    casa_chave: str
    telegram_chave: str
    tipster_opcoes: list[TipsterChoice]
    escolha_tipster_obrigatoria: bool
    concorrentes: int
    busca_incompleta: bool


class ReviewView(BaseModel):
    id: Identifier
    familia: Family
    motivo: Literal[
        "leitura_incompleta",
        "atribuicao_conta",
        "par_a_confirmar",
        "fontes_a_consolidar",
        "print_sem_aposta",
        "motivo_nao_suportado",
    ]
    estado: Literal["aberta", "resolvida"]
    criado_em: datetime
    resolvido_em: datetime | None
    precondicao: str
    acoes: list[Action]
    encaminhamento: Literal[
        "resolver_aposta",
        "resolver_conta",
        "resolver_par",
        "resolver_print",
        "suporte",
        "resolvida",
    ]
    resolver_url: str | None
    foto_url: str | None
    fontes: list[SourceEvidence]
    conta: AccountEvidence | None
    par: PairEvidence | None
    # Only safe, typed form fields for a print with no materialized bet.
    print_casa: str | None
    print_evento: str | None
    print_descricao: str | None
    print_odd: float | None
    print_stake_unidades: float | None


class ReviewPage(BaseModel):
    data: list[ReviewView]
    page: int
    page_size: int
    total_filtrado: int
    total_global: int
    por_familia_filtrado: dict[Family, int]
    observado_em: datetime
    politica_consumo: Literal["reiniciar_pagina_1_apos_resolver"]
    criterio_idade: Literal["intervalo_criado_em"]


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revisao_id: Identifier
    precondicao: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    acao: Action
    aposta_corrigida: CorrecaoDaRevisao | None = None
    tipster_choice: Literal["casa", "telegram"] | None = None
    conta_casa_id: Identifier | None = Field(
        default=None,
        description="Escolha explícita exata como string BIGINT; convertida somente no servidor. Não selecionar o primeiro candidato.",
    )

    @field_validator("revisao_id", "conta_casa_id")
    @classmethod
    def bigint(cls, value: str | None) -> str | None:
        if value is not None and int(value) > 2**63 - 1:
            raise ValueError("review_identifier_invalid")
        return value


class ReviewReceipt(BaseModel):
    operacao_id: UUID
    revisao_id: Identifier
    familia: Family
    acao: Action
    aposta_chave: str | None
    confirmado_em: datetime
    consultar_ate: datetime
    eventos_gravados: int


class ReviewError(BaseModel):
    detail: str
    campo: str | None = None
