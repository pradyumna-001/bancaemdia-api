"""Valor da unidade com vigência: leitura, troca idempotente e recuperação da intenção (#210)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
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
from bancaemdia.domain.registros import Unidade, Usuario
from bancaemdia.domain.temporal import VALOR_UNIDADE_PADRAO_CENTAVOS
from bancaemdia.repositories.unidade_repo import UnidadeRepo

PREFIXO = "/api/v1/unidade"
# R$ 100.000,00 por unidade: acima disso é quase certamente valor digitado em centavos a mais.
LIMITE_CENTAVOS = 10_000_000
CHAVE_EM_CONFLITO = "idempotency_key_conflict"

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        pattern=r"^[!-~]+$",
        description=(
            "Chave opaca escolhida pelo cliente antes do envio. Repetir a mesma chave e o mesmo "
            "corpo devolve o recibo original sem criar outra vigência."
        ),
    ),
]


class Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VigenciaSaida(Estrito):
    id: int | None
    valor_centavos: int
    origem: Literal["padrao", "usuario"]
    vigente_de: datetime | None
    vigente_ate: datetime | None
    vigente_de_local: str | None


class UnidadeSaida(Estrito):
    vigente: VigenciaSaida
    historico: list[VigenciaSaida]
    fuso_horario: str
    padrao_centavos: int
    limite_centavos: int


class VigenciaNova(Estrito):
    # StrictInt recusa 100.0, "10000" e true: valor ambíguo não vira dinheiro.
    valor_centavos: StrictInt = Field(gt=0, le=LIMITE_CENTAVOS)


class ReciboVigenciaSaida(Estrito):
    vigencia: VigenciaSaida
    anterior: VigenciaSaida
    vale_a_partir_de: datetime
    apostas_existentes_alteradas: Literal[0]
    fuso_horario: str


class CampoInvalidoSaida(Estrito):
    campo: str
    codigo: Literal[
        "obrigatorio", "inteiro", "positivo", "acima_do_limite", "desconhecido", "invalido"
    ]


class UnidadeInvalidaSaida(Estrito):
    detail: Literal["unidade_invalida"]
    campos: list[CampoInvalidoSaida]


VALIDACAO: OpenAPIResponses = {
    422: {
        "model": UnidadeInvalidaSaida,
        "description": "Pedido inválido; um código estável por campo, sem msg/input/ctx.",
    }
}
CODIGOS = {
    "missing": "obrigatorio",
    "int_type": "inteiro",
    "int_parsing": "inteiro",
    "int_from_float": "inteiro",
    "greater_than": "positivo",
    "less_than_equal": "acima_do_limite",
    "extra_forbidden": "desconhecido",
}


def validacao(error: RequestValidationError) -> JSONResponse:
    campos = []
    for issue in error.errors():
        loc = [str(parte) for parte in issue.get("loc", ()) if parte not in ("body", "header")]
        campo = loc[-1].lower() if loc else "corpo"
        campos.append({"campo": campo, "codigo": CODIGOS.get(issue.get("type", ""), "invalido")})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "unidade_invalida", "campos": campos},
        headers=_privado(),
    )


def _privado() -> dict[str, str]:
    return {"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"}


def _local(instante: datetime | None, fuso: str) -> str | None:
    return None if instante is None else instante.astimezone(ZoneInfo(fuso)).isoformat()


def _vigencia(unidade: Unidade | None, fuso: str) -> dict[str, Any]:
    if unidade is None:
        return {
            "id": None,
            "valor_centavos": VALOR_UNIDADE_PADRAO_CENTAVOS,
            "origem": "padrao",
            "vigente_de": None,
            "vigente_ate": None,
            "vigente_de_local": None,
        }
    return {
        "id": unidade.id,
        "valor_centavos": unidade.valor_centavos,
        "origem": "usuario",
        "vigente_de": unidade.vigente_de.isoformat(),
        "vigente_ate": None if unidade.vigente_ate is None else unidade.vigente_ate.isoformat(),
        "vigente_de_local": _local(unidade.vigente_de, fuso),
    }


def _hash(pedido: VigenciaNova) -> str:
    canonico = json.dumps({"valor_centavos": pedido.valor_centavos}, sort_keys=True)
    return hashlib.sha256(canonico.encode()).hexdigest()


@router.get("/api/v1/unidade", response_model=UnidadeSaida)
async def consultar_unidade(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    agora = await session.scalar(text("SELECT clock_timestamp()"))
    repo = UnidadeRepo()
    vigente = await repo.get_vigente(session, usuario.id, agora)
    historico = await repo.listar(session, usuario.id)
    fuso = usuario.fuso_horario
    return JSONResponse(
        {
            "vigente": _vigencia(vigente, fuso),
            "historico": [_vigencia(item, fuso) for item in historico],
            "fuso_horario": fuso,
            "padrao_centavos": VALOR_UNIDADE_PADRAO_CENTAVOS,
            "limite_centavos": LIMITE_CENTAVOS,
        },
        headers=_privado(),
    )


@router.post(
    "/api/v1/unidade/vigencias",
    status_code=status.HTTP_201_CREATED,
    response_model=ReciboVigenciaSaida,
    responses={
        409: {
            "model": ErrorResponse,
            "description": "idempotency_key_conflict: a chave já foi usada com outro corpo.",
        },
        **VALIDACAO,
    },
)
async def definir_unidade(
    pedido: VigenciaNova,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    await require_write_access(session, usuario.id)
    # Uma trava por usuário serializa trocas concorrentes e repetições da mesma chave: a segunda
    # vê a primeira já gravada e converge em vez de abrir duas vigências.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"unidade:{usuario.id}"},
    )
    requisicao_hash = _hash(pedido)
    repo = UnidadeRepo()
    gravada = await repo.get_por_chave(session, usuario.id, idempotency_key)
    if gravada is not None:
        await session.rollback()
        if gravada.requisicao_hash != requisicao_hash:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": CHAVE_EM_CONFLITO},
                headers=_privado(),
            )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content=gravada.recibo_json, headers=_privado()
        )

    # Relógio lido depois da trava: o now() da transação poderia ser anterior à troca que esperou.
    agora: datetime = await session.scalar(text("SELECT clock_timestamp()"))
    fuso = usuario.fuso_horario
    anterior = _vigencia(await repo.get_vigente(session, usuario.id, agora), fuso)
    await repo.encerrar_abertas(session, usuario.id, agora)
    nova = await repo.create(
        session,
        {
            "usuario_id": usuario.id,
            "valor_centavos": pedido.valor_centavos,
            "vigente_de": agora,
            "vigente_ate": None,
            "chave_idempotencia": idempotency_key,
            "requisicao_hash": requisicao_hash,
            "recibo_json": {},
        },
    )
    recibo = {
        "vigencia": _vigencia(nova, fuso),
        "anterior": anterior,
        "vale_a_partir_de": agora.isoformat(),
        # Apostas guardam o valor da unidade com que foram criadas; a troca nunca as reescreve.
        "apostas_existentes_alteradas": 0,
        "fuso_horario": fuso,
    }
    await session.execute(
        text("UPDATE unidades SET recibo_json = CAST(:recibo AS jsonb) WHERE id = :id"),
        {"recibo": json.dumps(recibo), "id": nova.id},
    )
    await session.commit()
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=recibo, headers=_privado())


@router.get(
    "/api/v1/unidade/vigencias/intencao",
    response_model=ReciboVigenciaSaida,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "intencao_nao_encontrada: nenhuma troca foi gravada com esta chave.",
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
    gravada = await UnidadeRepo().get_por_chave(session, usuario.id, idempotency_key)
    if gravada is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "intencao_nao_encontrada"},
            headers=_privado(),
        )
    return JSONResponse(content=gravada.recibo_json, headers=_privado())
