from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class Usuario:
    id: int
    email: str
    nome: str
    criado_em: datetime
    ativo: bool


@dataclass(frozen=True)
class ContaCasa:
    id: int
    usuario_id: int
    casa_id: int
    apelido: str
    desde: datetime | None
    ate: datetime | None
    ativa: bool


@dataclass(frozen=True)
class Unidade:
    id: int
    usuario_id: int
    valor_centavos: int
    vigente_de: datetime
    vigente_ate: datetime | None


@dataclass(frozen=True)
class Movimento:
    id: int
    usuario_id: int
    conta_casa_id: int | None
    tipo: str
    valor_centavos: int
    ocorrido_em: datetime
    descricao: str | None


@dataclass(frozen=True)
class Aposta:
    id: int
    usuario_id: int
    chave: str | None
    chat_id: int | None
    message_id: int | None
    ordem_na_mensagem: int
    midia_hash: str | None
    banca_id: int | None
    conta_casa_id: int | None
    tipster_id: int | None
    time_casa_id: int | None
    time_fora_id: int | None
    mercado_id: int | None
    competicao_id: int | None
    data_aposta: datetime | None
    data_jogo: datetime | None
    stake_unidades: float
    stake_centavos: int
    valor_aposta_centavos: int
    odd: float | None
    retorno_centavos: int | None
    estado: str
    origem: str
    freebet: bool
    duvida_de_par: bool
    parceira_chave: str | None
    duplicada_de: str | None
    revisao_grave: bool
    selecionada: bool
    criada_em: datetime
    atualizada_em: datetime


@dataclass(frozen=True)
class ApostasPorOrigem:
    origem: str
    apostas: int
    apostas_em_revisao: int
    mensagens: int
    mensagens_em_revisao: int


@dataclass(frozen=True)
class Evento:
    id: int
    usuario_id: int
    tipo: str
    payload_json: dict[str, Any]
    fonte: str
    confianca: float | None
    chat_id: int | None
    message_id: int | None
    criado_em: datetime
    aposta_chave: str | None


@dataclass(frozen=True)
class ColetaCasa:
    id: int
    usuario_id: int
    casa_id: int
    identidade: str
    hash_conteudo: str
    bruto_json: dict[str, Any]
    recebido_em: datetime
    processado_em: datetime | None


@dataclass(frozen=True)
class RevisaoPendente:
    id: int
    usuario_id: int
    midia_hash: str | None
    motivo: str
    extracao_bruta: dict[str, Any] | None
    criado_em: datetime
    resolvido_em: datetime | None


@dataclass(frozen=True)
class Upload:
    id: int
    job_id: UUID
    usuario_id: int
    filename: str
    chat_id: int | None
    status: str
    total_messages: int
    estimated_bets: int
    estimated_cost_usd: Decimal
    bets_processed: int
    bets_failed: int
    cost_usd: Decimal
    erro: str | None
    criado_em: datetime
    concluido_em: datetime | None


@dataclass(frozen=True)
class UploadBilhete:
    id: int
    upload_id: int
    usuario_id: int
    chat_id: int
    message_id: int
    midia_hash: str | None
    estado: str
    apostas: int
    custo_usd: Decimal
    enfileirado_em: datetime | None
    criado_em: datetime
