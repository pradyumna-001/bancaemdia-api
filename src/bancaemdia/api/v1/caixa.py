from datetime import date, datetime, time
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from pydantic.config import JsonDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user, get_current_user_snapshot
from bancaemdia.db.session import get_db, get_db_snapshot
from bancaemdia.domain.caixa_service import (
    BIGINT_MAX,
    BIGINT_MIN,
    MovimentoInvalidoError,
    planejar_movimento,
    saldo_da_conta,
)
from bancaemdia.domain.registros import Aposta, Banca, ContaCasa, LinhaExtrato, Movimento, Usuario
from bancaemdia.domain.temporal import SaldoDaCasa
from bancaemdia.observability.metrics import caixa_movimentos_total
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.banca_repo import BancaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.extrato_repo import ExtratoRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo

TAMANHO_PADRAO_DA_PAGINA = 50
TAMANHO_MAXIMO_DA_PAGINA = 100
PAGINA_MAXIMA = BIGINT_MAX // TAMANHO_MAXIMO_DA_PAGINA
TIPOS_DE_MOVIMENTO = frozenset({"DEPOSITO", "SAQUE", "TRANSFERENCIA", "BONUS", "AJUSTE"})
FUSO_DO_BRASIL = ZoneInfo("America/Sao_Paulo")
CONTA_OCUPADA = "esta conta está recebendo outro lançamento — tente de novo em instantes"
CONTA_NAO_ENCONTRADA = "não achei uma das contas informadas"
SEM_VINCULO_COM_BANCA = "as contas das casas ainda não têm vínculo com uma banca"

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)

TipoPublico = Literal["DEPOSITO", "SAQUE", "TRANSFERENCIA", "AJUSTE"]
Centavos = Annotated[int, Field(strict=True, ge=BIGINT_MIN, le=BIGINT_MAX)]
Identificador = Annotated[int, Field(strict=True, ge=1, le=BIGINT_MAX)]
ESQUEMA_CONDICIONAL_DO_MOVIMENTO: JsonDict = {
    "allOf": [
        {
            "if": {"properties": {"tipo": {"enum": ["DEPOSITO", "SAQUE"]}}, "required": ["tipo"]},
            "then": {
                "required": ["conta_casa_id"],
                "properties": {
                    "valor_centavos": {"exclusiveMinimum": 0},
                    "conta_casa_destino_id": {"type": "null"},
                },
            },
        },
        {
            "if": {"properties": {"tipo": {"const": "TRANSFERENCIA"}}, "required": ["tipo"]},
            "then": {
                "required": ["conta_casa_id", "conta_casa_destino_id"],
                "properties": {"valor_centavos": {"exclusiveMinimum": 0}},
            },
        },
        {
            "if": {"properties": {"tipo": {"const": "AJUSTE"}}, "required": ["tipo"]},
            "then": {
                "properties": {
                    "valor_centavos": {"not": {"const": 0}},
                    "conta_casa_destino_id": {"type": "null"},
                }
            },
        },
    ]
}


class MovimentoNovo(BaseModel):
    """Pedido de caixa com regras condicionais publicadas também no OpenAPI."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=ESQUEMA_CONDICIONAL_DO_MOVIMENTO)

    tipo: TipoPublico
    valor_centavos: Centavos = Field(description="Valor em centavos; positivo salvo para AJUSTE")
    conta_casa_id: Identificador | None = Field(
        default=None,
        description="Obrigatória para depósito, saque e origem da transferência",
    )
    conta_casa_destino_id: Identificador | None = Field(
        default=None,
        description="Obrigatória só para transferência e diferente da conta de origem",
    )
    ocorrido_em: datetime
    descricao: str | None = None

    @field_validator("tipo", mode="before")
    @classmethod
    def tipo_em_maiusculas(cls, valor: object) -> object:
        return valor.strip().upper() if isinstance(valor, str) else valor


class ErroSaida(BaseModel):
    detail: str


class ItemErroValidacaoSaida(BaseModel):
    loc: list[str | int]
    msg: str
    type: str
    input: JsonValue = None
    ctx: dict[str, JsonValue] | None = None


class ErroValidacaoSaida(BaseModel):
    detail: list[ItemErroValidacaoSaida]


class MovimentoSaida(BaseModel):
    id: int
    conta_casa_id: int | None
    tipo: Literal["DEPOSITO", "SAQUE", "TRANSFERENCIA", "BONUS", "AJUSTE"]
    valor_centavos: int
    transferencia_id: UUID | None
    ocorrido_em: datetime
    descricao: str | None


class MovimentoCriadoSaida(BaseModel):
    tipo: TipoPublico
    transferencia_id: UUID | None
    movimentos: list[MovimentoSaida]


class PaginacaoSaida(BaseModel):
    page: int
    page_size: int
    total: int


class MovimentosPaginaSaida(BaseModel):
    data: list[MovimentoSaida]
    pagination: PaginacaoSaida


class SaldoContaSaida(BaseModel):
    conta_casa_id: int
    casa_id: int
    apelido: str
    saldo_atual_centavos: int | None
    saldo_confiavel: bool
    depositado_centavos: int
    sacado_centavos: int
    bonus_centavos: int
    movido_centavos: int
    apostado_centavos: int
    retornado_centavos: int
    em_jogo_centavos: int
    apostas_pendentes: int
    movimentos: int
    desde: datetime | None
    deposito_faltante_centavos: int


class SaldoBancaSaida(BaseModel):
    id: int
    nome: str
    saldo_inicial_centavos: int | None
    saldo_total_centavos: None
    motivo_saldo_indisponivel: str


class SaldoSaida(BaseModel):
    data_corte: date | None
    contas: list[SaldoContaSaida]
    saldo_conhecido_centavos: int
    saldo_total_centavos: int | None
    saldo_total_escopo: Literal["contas_casa"]
    contas_sem_saldo_confiavel: int
    bancas: list[SaldoBancaSaida]


class MovimentoExtratoSaida(MovimentoSaida):
    origem: Literal["movimento"]
    data_referencia: datetime


class ApostaExtratoSaida(BaseModel):
    origem: Literal["aposta"]
    id: int
    chave: str | None
    tipo: Literal["APOSTA_LIQUIDADA"]
    conta_casa_id: int | None
    estado: Literal["GREEN", "RED", "ANULADA", "MEIO_GREEN", "MEIO_RED", "CASHOUT"]
    stake_centavos: int
    retorno_centavos: int | None
    resultado_liquido_centavos: int | None
    revisao_grave: bool
    data_referencia: datetime | None
    data_referencia_origem: Literal["data_aposta", "criada_em"]
    liquidada_em: None


ItemExtrato = Annotated[
    MovimentoExtratoSaida | ApostaExtratoSaida,
    Field(discriminator="origem"),
]


class ExtratoSaida(BaseModel):
    data: list[ItemExtrato]
    pagination: PaginacaoSaida


class TipoMovimentoConsulta(StrEnum):
    DEPOSITO = "DEPOSITO"
    SAQUE = "SAQUE"
    TRANSFERENCIA = "TRANSFERENCIA"
    BONUS = "BONUS"
    AJUSTE = "AJUSTE"

    @classmethod
    def _missing_(cls, valor: object) -> "TipoMovimentoConsulta | None":
        if isinstance(valor, str):
            normalizado = valor.strip().upper()
            return next((item for item in cls if item.value == normalizado), None)
        return None


def erro(status_code: int, mensagem: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": mensagem})


def _com_fuso(valor: datetime) -> datetime:
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=FUSO_DO_BRASIL)


def _instante(valor: datetime | date | None) -> datetime | None:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return _com_fuso(valor)
    return datetime.combine(valor, time.min, tzinfo=FUSO_DO_BRASIL)


def _quando(valor: datetime | date | None) -> str | None:
    instante = _instante(valor)
    return None if instante is None else instante.isoformat()


def _uuid_texto(valor: object) -> str | None:
    return None if valor is None else str(valor)


def linha_do_movimento(movimento: Movimento) -> dict[str, object]:
    return {
        "id": movimento.id,
        "conta_casa_id": movimento.conta_casa_id,
        "tipo": movimento.tipo,
        "valor_centavos": movimento.valor_centavos,
        "transferencia_id": _uuid_texto(getattr(movimento, "transferencia_id", None)),
        "ocorrido_em": _quando(movimento.ocorrido_em),
        "descricao": movimento.descricao,
    }


async def _travar(session: AsyncSession, usuario_id: int, conta_ids: list[int]) -> bool:
    # A ordem é parte do contrato: duas transferências inversas tentam as mesmas travas na mesma
    # sequência. O `try` não espera pelo timeout de cinco segundos do primário.
    chaves = [f"caixa:{usuario_id}:{conta_id}" for conta_id in sorted(set(conta_ids))]
    if not chaves:
        chaves = [f"caixa:{usuario_id}:global"]
    for chave in chaves:
        livre = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:chave, 0))"),
            {"chave": chave},
        )
        if not livre:
            return False
    return True


@router.post(
    "/api/v1/caixa",
    status_code=status.HTTP_201_CREATED,
    response_model=MovimentoCriadoSaida,
    responses={
        404: {"model": ErroSaida, "description": "Conta não encontrada"},
        409: {"model": ErroSaida, "description": "Conta ocupada por outro lançamento"},
        422: {
            "model": ErroSaida | ErroValidacaoSaida,
            "description": "Pedido malformado ou movimento inválido",
        },
    },
)
async def registrar_movimento(
    pedido: MovimentoNovo,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    try:
        plano = planejar_movimento(
            pedido.tipo,
            pedido.valor_centavos,
            pedido.conta_casa_id,
            pedido.conta_casa_destino_id,
        )
    except MovimentoInvalidoError as recusa:
        await session.rollback()
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, str(recusa))

    conta_ids = sorted({
        lancamento.conta_casa_id
        for lancamento in plano.lancamentos
        if lancamento.conta_casa_id is not None
    })
    if not await _travar(session, usuario.id, conta_ids):
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, CONTA_OCUPADA)

    contas = (
        await ContaCasaRepo().get_many_for_update(session, usuario.id, conta_ids)
        if conta_ids
        else []
    )
    if {conta.id for conta in contas} != set(conta_ids):
        # Sob RLS, conta de outra pessoa é invisível. Responder 404 não confirma que ela existe.
        await session.rollback()
        return erro(status.HTTP_404_NOT_FOUND, CONTA_NAO_ENCONTRADA)

    ocorrido_em = _com_fuso(pedido.ocorrido_em)
    movimentos: list[Movimento] = []
    repositorio = MovimentoRepo()
    try:
        for lancamento in plano.lancamentos:
            movimentos.append(
                await repositorio.append(
                    session,
                    {
                        "usuario_id": usuario.id,
                        "conta_casa_id": lancamento.conta_casa_id,
                        "tipo": lancamento.tipo,
                        "valor_centavos": lancamento.valor_centavos,
                        "transferencia_id": plano.transferencia_id,
                        "ocorrido_em": ocorrido_em,
                        "descricao": pedido.descricao,
                    },
                )
            )

        payload = {
            "tipo": plano.tipo,
            "transferencia_id": _uuid_texto(plano.transferencia_id),
            "ocorrido_em": ocorrido_em.isoformat(),
            "descricao": pedido.descricao,
            "lancamentos": [
                {
                    "movimento_id": movimento.id,
                    "conta_casa_id": movimento.conta_casa_id,
                    "tipo": movimento.tipo,
                    "valor_centavos": movimento.valor_centavos,
                }
                for movimento in movimentos
            ],
        }
        await EventoRepo().append(
            session,
            {
                "usuario_id": usuario.id,
                "tipo": "MOVIMENTO_REGISTRADO",
                "fonte": "manual",
                "payload_json": payload,
                "confianca": None,
                "chat_id": None,
                "message_id": None,
                "aposta_chave": None,
            },
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    for movimento in movimentos:
        caixa_movimentos_total.labels(tipo=movimento.tipo).inc()
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "tipo": plano.tipo,
            "transferencia_id": _uuid_texto(plano.transferencia_id),
            "movimentos": [linha_do_movimento(movimento) for movimento in movimentos],
        },
    )


@router.get(
    "/api/v1/caixa",
    response_model=MovimentosPaginaSaida,
    responses={
        422: {
            "model": ErroSaida | ErroValidacaoSaida,
            "description": "Filtro ou paginação inválidos",
        }
    },
)
async def listar_movimentos(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    conta_casa_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    tipo: TipoMovimentoConsulta | None = None,
    desde: datetime | None = None,
    ate: datetime | None = None,
    page: Annotated[int, Query(ge=1, le=PAGINA_MAXIMA)] = 1,
    page_size: Annotated[int, Query(ge=1, le=TAMANHO_MAXIMO_DA_PAGINA)] = TAMANHO_PADRAO_DA_PAGINA,
) -> JSONResponse:
    tipo_normalizado = None if tipo is None else str(tipo).strip().upper()
    if tipo_normalizado is not None and tipo_normalizado not in TIPOS_DE_MOVIMENTO:
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, f"tipo de movimento desconhecido: {tipo}")
    movimentos, total = await MovimentoRepo().list_page(
        session,
        usuario.id,
        {
            "conta_casa_id": conta_casa_id,
            "tipo": tipo_normalizado,
            "desde": _instante(desde),
            "ate": _instante(ate),
        },
        page,
        page_size,
    )
    return JSONResponse({
        "data": [linha_do_movimento(movimento) for movimento in movimentos],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    })


def _linha_do_saldo(conta: ContaCasa, saldo: SaldoDaCasa) -> dict[str, object]:
    return {
        "conta_casa_id": conta.id,
        "casa_id": conta.casa_id,
        "apelido": conta.apelido,
        "saldo_atual_centavos": saldo.saldo_centavos,
        "saldo_confiavel": saldo.saldo_centavos is not None,
        "depositado_centavos": saldo.depositado_centavos,
        "sacado_centavos": saldo.sacado_centavos,
        "bonus_centavos": saldo.bonus_centavos,
        "movido_centavos": saldo.movido_centavos,
        "apostado_centavos": saldo.apostado_centavos,
        "retornado_centavos": saldo.retornado_centavos,
        "em_jogo_centavos": saldo.em_jogo_centavos,
        "apostas_pendentes": saldo.apostas_pendentes,
        "movimentos": saldo.movimentos,
        "desde": _quando(saldo.desde),
        "deposito_faltante_centavos": saldo.deposito_faltante_centavos,
    }


def _linha_da_banca(banca: Banca) -> dict[str, object]:
    # Sem uma relação entre banca e conta da casa, qualquer soma seria uma escolha inventada.
    return {
        "id": banca.id,
        "nome": banca.nome,
        "saldo_inicial_centavos": banca.saldo_inicial_centavos,
        "saldo_total_centavos": None,
        "motivo_saldo_indisponivel": SEM_VINCULO_COM_BANCA,
    }


@router.get(
    "/api/v1/caixa/saldo",
    response_model=SaldoSaida,
)
async def consultar_saldo(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    data_corte: date | None = None,
) -> JSONResponse:
    contas = await ContaCasaRepo().list_by_usuario(session, usuario.id)
    movimentos = await MovimentoRepo().list_by_usuario(session, usuario.id)
    apostas = await ApostaRepo().list_by_usuario(session, usuario.id)
    bancas = await BancaRepo().list_by_usuario(session, usuario.id)

    apostas_por_conta: dict[int, list[Aposta]] = {}
    for aposta in apostas:
        if aposta.conta_casa_id is not None:
            apostas_por_conta.setdefault(aposta.conta_casa_id, []).append(aposta)
    movimentos_por_conta: dict[int, list[Movimento]] = {}
    for movimento in movimentos:
        if movimento.conta_casa_id is not None:
            movimentos_por_conta.setdefault(movimento.conta_casa_id, []).append(movimento)

    linhas = [
        _linha_do_saldo(
            conta,
            saldo_da_conta(
                conta.id,
                apostas_por_conta.get(conta.id, []),
                movimentos_por_conta.get(conta.id, []),
                data_corte,
            ),
        )
        for conta in contas
    ]
    saldos = [linha["saldo_atual_centavos"] for linha in linhas]
    conhecidos = [valor for valor in saldos if isinstance(valor, int)]
    desconhecidos = len(saldos) - len(conhecidos)
    saldo_conhecido = sum(conhecidos)
    return JSONResponse({
        "data_corte": None if data_corte is None else data_corte.isoformat(),
        "contas": linhas,
        "saldo_conhecido_centavos": saldo_conhecido,
        "saldo_total_centavos": None if desconhecidos else saldo_conhecido,
        "saldo_total_escopo": "contas_casa",
        "contas_sem_saldo_confiavel": desconhecidos,
        "bancas": [_linha_da_banca(banca) for banca in bancas],
    })


def linha_do_extrato(item: LinhaExtrato) -> dict[str, object]:
    if item.origem == "movimento":
        return {
            "origem": "movimento",
            "id": item.id,
            "conta_casa_id": item.conta_casa_id,
            "tipo": item.tipo,
            "valor_centavos": item.valor_centavos,
            "transferencia_id": _uuid_texto(item.transferencia_id),
            "ocorrido_em": _quando(item.data_referencia),
            "descricao": item.descricao,
            "data_referencia": _quando(item.data_referencia),
        }
    return {
        "origem": "aposta",
        "id": item.id,
        "chave": item.chave,
        "tipo": item.tipo,
        "conta_casa_id": item.conta_casa_id,
        "estado": item.estado,
        "stake_centavos": item.stake_centavos,
        "retorno_centavos": item.retorno_centavos,
        "resultado_liquido_centavos": item.resultado_liquido_centavos,
        "revisao_grave": item.revisao_grave,
        "data_referencia": _quando(item.data_referencia),
        "data_referencia_origem": item.data_referencia_origem,
        # A projeção atual não guarda quando o resultado foi registrado. A data da aposta é útil
        # para ordenar, mas não é renomeada como liquidação.
        "liquidada_em": None,
    }


@router.get(
    "/api/v1/caixa/extrato",
    response_model=ExtratoSaida,
)
async def consultar_extrato(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    conta_casa_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    desde: datetime | None = None,
    ate: datetime | None = None,
    page: Annotated[int, Query(ge=1, le=PAGINA_MAXIMA)] = 1,
    page_size: Annotated[int, Query(ge=1, le=TAMANHO_MAXIMO_DA_PAGINA)] = TAMANHO_PADRAO_DA_PAGINA,
) -> JSONResponse:
    itens, total = await ExtratoRepo().list_page(
        session,
        usuario.id,
        {
            "conta_casa_id": conta_casa_id,
            "desde": _instante(desde),
            "ate": _instante(ate),
        },
        page,
        page_size,
    )
    return JSONResponse({
        "data": [linha_do_extrato(item) for item in itens],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    })
