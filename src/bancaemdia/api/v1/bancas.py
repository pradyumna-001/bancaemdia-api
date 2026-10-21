"""Bancas do site: cadastro, capital inicial e arquivamento, com escritas idempotentes (#211)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Path, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    ErrorResponse,
    OpenAPIResponses,
)
from bancaemdia.api.deps import get_current_user
from bancaemdia.db.session import get_db, get_db_primary
from bancaemdia.domain.access import require_write_access
from bancaemdia.domain.registros import Banca, Usuario
from bancaemdia.repositories.banca_repo import BancaRepo, BancaRequisicaoRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo

PREFIXO = "/api/v1/bancas"
BIGINT_MAX = 9_223_372_036_854_775_807
# R$ 1 bilhão: acima disso o valor só pode ser erro de digitação.
LIMITE_CENTAVOS = 100_000_000_000

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)
Nome = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Capital = Annotated[StrictInt, Field(ge=0, le=LIMITE_CENTAVOS)]
BancaId = Annotated[int, Path(ge=1, le=BIGINT_MAX)]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        pattern=r"^[!-~]+$",
        description=(
            "Chave opaca escolhida pelo cliente antes do envio. Repetir a mesma chave e o mesmo "
            "pedido devolve a resposta original sem repetir o efeito."
        ),
    ),
]


class Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContaVinculadaSaida(Estrito):
    id: int
    casa_id: int
    apelido: str


class BancaSaida(Estrito):
    id: int
    nome: str
    estado: Literal["ativa", "arquivada"]
    arquivada_em: datetime | None
    saldo_inicial_centavos: int | None
    contas: list[ContaVinculadaSaida]


class BancasSaida(Estrito):
    bancas: list[BancaSaida]


class BancaNova(Estrito):
    nome: Nome
    # Ausente ou null: capital não informado; a progressão continua null, nunca zero.
    saldo_inicial_centavos: Capital | None = None


class BancaAlteracao(Estrito):
    nome: Nome | None = None
    saldo_inicial_centavos: Capital | None = None

    @model_validator(mode="after")
    def algum_campo(self) -> BancaAlteracao:
        if not self.model_fields_set:
            raise ValueError("vazio")
        if "nome" in self.model_fields_set and self.nome is None:
            raise ValueError("nome")
        return self


class IntencaoSaida(Estrito):
    operacao: Literal["CRIAR", "ALTERAR", "ARQUIVAR"]
    status_code: int
    resposta: BancaSaida


class CampoInvalidoSaida(Estrito):
    campo: str
    codigo: Literal[
        "obrigatorio",
        "inteiro",
        "nao_negativo",
        "acima_do_limite",
        "tamanho",
        "vazio",
        "desconhecido",
        "invalido",
    ]


class BancaInvalidaSaida(Estrito):
    detail: Literal["banca_invalida"]
    campos: list[CampoInvalidoSaida]


VALIDACAO: OpenAPIResponses = {
    422: {
        "model": BancaInvalidaSaida,
        "description": "Pedido inválido; um código estável por campo, sem msg/input/ctx.",
    }
}
CONFLITO = "idempotency_key_conflict"
CODIGOS = {
    "missing": "obrigatorio",
    "int_type": "inteiro",
    "int_parsing": "inteiro",
    "int_from_float": "inteiro",
    "greater_than_equal": "nao_negativo",
    "less_than_equal": "acima_do_limite",
    "string_too_short": "tamanho",
    "string_too_long": "tamanho",
    "extra_forbidden": "desconhecido",
}


def validacao(error: RequestValidationError) -> JSONResponse:
    campos = []
    for issue in error.errors():
        loc = [str(parte) for parte in issue.get("loc", ()) if parte not in ("body", "header")]
        mensagem = str(issue.get("msg", ""))
        if issue.get("type") == "value_error" and mensagem.endswith("vazio"):
            campos.append({"campo": "corpo", "codigo": "vazio"})
            continue
        if issue.get("type") == "value_error" and mensagem.endswith("nome"):
            campos.append({"campo": "nome", "codigo": "obrigatorio"})
            continue
        campo = loc[-1].lower() if loc else "corpo"
        campos.append({"campo": campo, "codigo": CODIGOS.get(issue.get("type", ""), "invalido")})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "banca_invalida", "campos": campos},
        headers=_privado(),
    )


def _privado() -> dict[str, str]:
    return {"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"}


def _falha(status_code: int, detalhe: str) -> tuple[int, dict[str, Any]]:
    return status_code, {"detail": detalhe}


async def _linha(session: AsyncSession, usuario_id: int, banca: Banca) -> dict[str, Any]:
    contas = await ContaCasaRepo().list_by_usuario(session, usuario_id)
    return _serializar(banca, [conta for conta in contas if conta.banca_id == banca.id])


def _serializar(banca: Banca, contas: list[Any]) -> dict[str, Any]:
    return {
        "id": banca.id,
        "nome": banca.nome,
        "estado": "ativa" if banca.arquivada_em is None else "arquivada",
        "arquivada_em": None if banca.arquivada_em is None else banca.arquivada_em.isoformat(),
        "saldo_inicial_centavos": banca.saldo_inicial_centavos,
        "contas": [
            {"id": conta.id, "casa_id": conta.casa_id, "apelido": conta.apelido} for conta in contas
        ],
    }


async def _idempotente(
    session: AsyncSession,
    usuario: Usuario,
    chave: str,
    operacao: str,
    banca_id: int | None,
    pedido: dict[str, Any],
    efeito: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
) -> JSONResponse:
    await require_write_access(session, usuario.id)
    # Uma trava por usuário: repetição concorrente da mesma chave espera a primeira e devolve o
    # recibo dela; nomes não disputam a unicidade entre duas escritas simultâneas.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"bancas:{usuario.id}"},
    )
    canonico = json.dumps(
        {"operacao": operacao, "banca_id": banca_id, "pedido": pedido}, sort_keys=True
    )
    pedido_hash = hashlib.sha256(canonico.encode()).hexdigest()
    requisicoes = BancaRequisicaoRepo()
    gravada = await requisicoes.get(session, usuario.id, chave)
    if gravada is not None:
        # Lidos antes do rollback, que expira o objeto ORM.
        mesmo_pedido, resposta, codigo = (
            gravada.pedido_hash == pedido_hash,
            gravada.resposta,
            gravada.status_code,
        )
        await session.rollback()
        if not mesmo_pedido:
            return JSONResponse({"detail": CONFLITO}, status_code=409, headers=_privado())
        return JSONResponse(resposta, status_code=codigo, headers=_privado())
    codigo, corpo = await efeito()
    if codigo >= 400:
        # Recusa não é gravada: a mesma intenção pode ser repetida depois de resolver o conflito.
        await session.rollback()
        return JSONResponse(corpo, status_code=codigo, headers=_privado())
    await requisicoes.append(
        session,
        {
            "usuario_id": usuario.id,
            "operacao": operacao,
            "banca_id": corpo["id"],
            "chave": chave,
            "pedido_hash": pedido_hash,
            "status_code": codigo,
            "resposta": corpo,
        },
    )
    await session.commit()
    return JSONResponse(corpo, status_code=codigo, headers=_privado())


@router.get("/api/v1/bancas", response_model=BancasSaida)
async def listar_bancas(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    bancas = await BancaRepo().list_by_usuario(session, usuario.id)
    contas = await ContaCasaRepo().list_by_usuario(session, usuario.id)
    return JSONResponse(
        {
            "bancas": [
                _serializar(banca, [conta for conta in contas if conta.banca_id == banca.id])
                for banca in bancas
            ]
        },
        headers=_privado(),
    )


@router.post(
    "/api/v1/bancas",
    status_code=status.HTTP_201_CREATED,
    response_model=BancaSaida,
    responses={
        409: {
            "model": ErrorResponse,
            "description": "nome_em_uso ou idempotency_key_conflict.",
        },
        **VALIDACAO,
    },
)
async def criar_banca(
    pedido: BancaNova,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    repo = BancaRepo()

    async def efeito() -> tuple[int, dict[str, Any]]:
        if await repo.nome_em_uso(session, usuario.id, pedido.nome):
            return _falha(status.HTTP_409_CONFLICT, "nome_em_uso")
        banca = await repo.create(session, usuario.id, pedido.nome, pedido.saldo_inicial_centavos)
        return status.HTTP_201_CREATED, _serializar(banca, [])

    return await _idempotente(
        session, usuario, idempotency_key, "CRIAR", None, pedido.model_dump(), efeito
    )


@router.patch(
    "/api/v1/bancas/{banca_id}",
    response_model=BancaSaida,
    responses={
        404: {"model": ErrorResponse, "description": "banca_nao_encontrada."},
        409: {
            "model": ErrorResponse,
            "description": "nome_em_uso, banca_arquivada ou idempotency_key_conflict.",
        },
        **VALIDACAO,
    },
)
async def alterar_banca(
    banca_id: BancaId,
    pedido: BancaAlteracao,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    repo = BancaRepo()
    # Só os campos enviados mudam; saldo_inicial_centavos null apaga o capital informado.
    valores = pedido.model_dump(include=pedido.model_fields_set)

    async def efeito() -> tuple[int, dict[str, Any]]:
        banca = await repo.get_by_id(session, usuario.id, banca_id)
        if banca is None:
            return _falha(status.HTTP_404_NOT_FOUND, "banca_nao_encontrada")
        if banca.arquivada_em is not None:
            return _falha(status.HTTP_409_CONFLICT, "banca_arquivada")
        nome = valores.get("nome")
        if isinstance(nome, str) and await repo.nome_em_uso(session, usuario.id, nome, banca_id):
            return _falha(status.HTTP_409_CONFLICT, "nome_em_uso")
        alterada = await repo.update(session, usuario.id, banca_id, valores)
        return status.HTTP_200_OK, await _linha(session, usuario.id, alterada)

    return await _idempotente(
        session, usuario, idempotency_key, "ALTERAR", banca_id, valores, efeito
    )


@router.post(
    "/api/v1/bancas/{banca_id}/arquivar",
    response_model=BancaSaida,
    responses={
        404: {"model": ErrorResponse, "description": "banca_nao_encontrada."},
        409: {
            "model": ErrorResponse,
            "description": (
                "banca_com_contas (desvincule as contas antes), banca_arquivada ou "
                "idempotency_key_conflict."
            ),
        },
        **VALIDACAO,
    },
)
async def arquivar_banca(
    banca_id: BancaId,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    repo = BancaRepo()

    async def efeito() -> tuple[int, dict[str, Any]]:
        banca = await repo.get_by_id(session, usuario.id, banca_id)
        if banca is None:
            return _falha(status.HTTP_404_NOT_FOUND, "banca_nao_encontrada")
        if banca.arquivada_em is not None:
            return _falha(status.HTTP_409_CONFLICT, "banca_arquivada")
        # Conta ativa continua movendo dinheiro: arquivar com vínculo esconderia esse saldo.
        if await repo.contas_vinculadas(session, usuario.id, banca_id):
            return _falha(status.HTTP_409_CONFLICT, "banca_com_contas")
        quando = await session.scalar(text("SELECT clock_timestamp()"))
        arquivada = await repo.archive(session, usuario.id, banca_id, quando)
        return status.HTTP_200_OK, _serializar(arquivada, [])

    return await _idempotente(session, usuario, idempotency_key, "ARQUIVAR", banca_id, {}, efeito)


@router.get(
    "/api/v1/bancas/intencao",
    response_model=IntencaoSaida,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "intencao_nao_encontrada: nenhuma escrita foi gravada com esta chave.",
        },
        **VALIDACAO,
    },
)
async def recuperar_intencao(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    # Primário: a resposta perdida pode ser de um commit que a réplica ainda não recebeu.
    gravada = await BancaRequisicaoRepo().get(session, usuario.id, idempotency_key)
    if gravada is None:
        return JSONResponse(
            {"detail": "intencao_nao_encontrada"}, status_code=404, headers=_privado()
        )
    return JSONResponse(
        {
            "operacao": gravada.operacao,
            "status_code": gravada.status_code,
            "resposta": gravada.resposta,
        },
        headers=_privado(),
    )
