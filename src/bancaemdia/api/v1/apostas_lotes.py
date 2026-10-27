"""Apagar todas as apostas em lote, com confirmação do servidor e restauração exata (#212)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse, OpenAPIResponses
from bancaemdia.api.deps import get_current_user
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db, get_db_primary
from bancaemdia.domain.access import require_write_access
from bancaemdia.domain.aposta_service import FONTE_DA_PESSOA
from bancaemdia.domain.materializar import MOTIVO_APAGADA
from bancaemdia.domain.registros import Usuario
from bancaemdia.repositories.cruzamento_candidato import CruzamentoCandidatoRepo

PREFIXO = "/api/v1/apostas/lotes"
VALIDADE_SEGUNDOS = 600
# Motivos de revisão que a exclusão individual também não fecha: dependem de decisão da pessoa.
REVISOES_PRESERVADAS = ("conta_pendente", "consolidacao_pendente", "cruzamento_candidato")

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
            "pedido devolve o recibo original sem repetir o efeito."
        ),
    ),
]


class Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviaSaida(Estrito):
    escopo: Literal["apostas"]
    total: int
    frase: str
    confirmacao: str
    expira_em: datetime


class LoteNovo(Estrito):
    acao: Literal["apagar_todas"]
    confirmacao: str = Field(min_length=1, max_length=512)
    frase: str = Field(min_length=1, max_length=64)


class LoteSaida(Estrito):
    lote_id: UUID
    escopo: Literal["apostas"]
    apostas_afetadas: int
    criado_em: datetime
    restaurado_em: datetime | None
    apostas_restauradas: int | None


class IntencaoSaida(Estrito):
    operacao: Literal["APAGAR", "RESTAURAR"]
    status_code: int
    resposta: LoteSaida


class CampoInvalidoSaida(Estrito):
    campo: str
    codigo: Literal["obrigatorio", "tamanho", "desconhecido", "invalido"]


class LoteInvalidoSaida(Estrito):
    detail: Literal["lote_invalido"]
    campos: list[CampoInvalidoSaida]


type LoteRecusadoSaida = LoteInvalidoSaida | ErrorResponse

VALIDACAO: OpenAPIResponses = {
    422: {
        "model": LoteRecusadoSaida,
        "description": (
            "lote_invalido (código por campo), frase_incorreta ou confirmacao_invalida; sem "
            "msg/input/ctx."
        ),
    }
}
CODIGOS = {
    "missing": "obrigatorio",
    "string_too_short": "tamanho",
    "string_too_long": "tamanho",
    "extra_forbidden": "desconhecido",
}


def validacao(error: RequestValidationError) -> JSONResponse:
    campos = []
    for issue in error.errors():
        loc = [str(parte) for parte in issue.get("loc", ()) if parte not in ("body", "header")]
        campo = loc[-1].lower() if loc else "corpo"
        campos.append({"campo": campo, "codigo": CODIGOS.get(issue.get("type", ""), "invalido")})
    return JSONResponse(
        {"detail": "lote_invalido", "campos": campos}, status_code=422, headers=_privado()
    )


def _privado() -> dict[str, str]:
    return {"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"}


def _recusa(status_code: int, detalhe: str) -> JSONResponse:
    return JSONResponse({"detail": detalhe}, status_code=status_code, headers=_privado())


def _frase(total: int) -> str:
    return f"APAGAR {total} APOSTAS"


def _chave_hmac() -> bytes:
    # Domínio próprio: o mesmo segredo nunca assina dois tipos de prova.
    segredo = get_settings().JWT_SECRET_KEY.encode()
    return hashlib.sha256(b"bancaemdia:apagar-apostas:v1:" + segredo).digest()


def _assinar(dados: dict[str, Any]) -> str:
    corpo = base64.urlsafe_b64encode(json.dumps(dados, sort_keys=True).encode()).decode()
    assinatura = hmac.new(_chave_hmac(), corpo.encode(), hashlib.sha256).hexdigest()
    return f"{corpo}.{assinatura}"


def _abrir(token: str) -> dict[str, Any] | None:
    corpo, _, assinatura = token.partition(".")
    esperada = hmac.new(_chave_hmac(), corpo.encode(), hashlib.sha256).hexdigest()
    if not assinatura or not hmac.compare_digest(assinatura, esperada):
        return None
    try:
        dados = json.loads(base64.urlsafe_b64decode(corpo.encode()))
    except (binascii.Error, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


async def _ativas(session: AsyncSession, usuario_id: int) -> tuple[int, str]:
    # Impressão digital do conjunto: qualquer aposta criada, apagada ou restaurada depois da
    # prévia muda o resultado, e a confirmação antiga deixa de valer.
    linha = (
        await session.execute(
            text(
                "SELECT count(*), md5(COALESCE(string_agg(chave, ',' ORDER BY chave), '')) "
                "FROM apostas WHERE usuario_id = :uid AND selecionada AND chave IS NOT NULL"
            ),
            {"uid": usuario_id},
        )
    ).one()
    return int(linha[0]), str(linha[1])


def _lote(linha: models.ApostaLote, restauracao: models.ApostaLote | None) -> dict[str, Any]:
    original = dict(linha.resposta)
    if restauracao is not None:
        original["restaurado_em"] = restauracao.resposta["restaurado_em"]
        original["apostas_restauradas"] = restauracao.resposta["apostas_restauradas"]
    return original


async def _idempotente(
    session: AsyncSession,
    usuario: Usuario,
    chave: str,
    pedido: dict[str, Any],
    efeito: Callable[[], Awaitable[JSONResponse | tuple[int, dict[str, Any], dict[str, Any]]]],
) -> JSONResponse:
    await require_write_access(session, usuario.id)
    # Outro lote em curso recusa na hora em vez de enfileirar milhares de linhas atrás dele.
    livre = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"apostas-lote:{usuario.id}"},
    )
    if not livre:
        await session.rollback()
        return _recusa(status.HTTP_409_CONFLICT, "lote_em_curso")
    pedido_hash = hashlib.sha256(json.dumps(pedido, sort_keys=True).encode()).hexdigest()
    gravada = (
        await session.execute(
            select(models.ApostaLote).where(
                models.ApostaLote.usuario_id == usuario.id, models.ApostaLote.chave == chave
            )
        )
    ).scalar_one_or_none()
    if gravada is not None:
        mesmo, codigo, resposta = (
            gravada.pedido_hash == pedido_hash,
            gravada.status_code,
            gravada.resposta,
        )
        await session.rollback()
        if not mesmo:
            return _recusa(status.HTTP_409_CONFLICT, "idempotency_key_conflict")
        return JSONResponse(resposta, status_code=codigo, headers=_privado())
    # A mesma trava de todo escritor de apostas: nenhuma leitura, correção ou coleta grava no
    # meio do lote, e o lote não grava no meio delas.
    await CruzamentoCandidatoRepo().lock(session, usuario.id)
    resultado = await efeito()
    if isinstance(resultado, JSONResponse):
        await session.rollback()
        return resultado
    codigo, resposta, linha = resultado
    session.add(
        models.ApostaLote(usuario_id=usuario.id, chave=chave, pedido_hash=pedido_hash, **linha)
    )
    await session.commit()
    return JSONResponse(resposta, status_code=codigo, headers=_privado())


async def _eventos(
    session: AsyncSession, usuario_id: int, chaves: list[str], tipo: str, payload: dict[str, Any]
) -> None:
    # Um evento por aposta, o mesmo da exclusão/restauração individual: o replay reconstrói o
    # lote sem saber que ele existiu.
    await session.execute(
        text(
            "INSERT INTO eventos (usuario_id, tipo, fonte, payload_json, aposta_chave) "
            "SELECT :uid, :tipo, :fonte, CAST(:payload AS jsonb), chave "
            "FROM unnest(CAST(:chaves AS text[])) AS chave"
        ),
        {
            "uid": usuario_id,
            "tipo": tipo,
            "fonte": FONTE_DA_PESSOA,
            "payload": json.dumps(payload),
            "chaves": chaves,
        },
    )


@router.get(f"{PREFIXO}/previa-apagar", response_model=PreviaSaida)
async def previa_apagar(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> JSONResponse:
    total, digital = await _ativas(session, usuario.id)
    expira = int(time.time()) + VALIDADE_SEGUNDOS
    return JSONResponse(
        {
            "escopo": "apostas",
            "total": total,
            "frase": _frase(total),
            "confirmacao": _assinar({"u": usuario.id, "n": total, "f": digital, "e": expira}),
            "expira_em": datetime.fromtimestamp(expira, UTC).isoformat(),
        },
        headers=_privado(),
    )


@router.post(
    PREFIXO,
    status_code=status.HTTP_201_CREATED,
    response_model=LoteSaida,
    responses={
        409: {
            "model": ErrorResponse,
            "description": (
                "confirmacao_desatualizada, confirmacao_expirada, nada_a_apagar, lote_em_curso ou "
                "idempotency_key_conflict."
            ),
        },
        **VALIDACAO,
    },
)
async def apagar_todas(
    pedido: LoteNovo,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    async def efeito() -> JSONResponse | tuple[int, dict[str, Any], dict[str, Any]]:
        prova = _abrir(pedido.confirmacao)
        if prova is None or prova.get("u") != usuario.id:
            return _recusa(status.HTTP_422_UNPROCESSABLE_ENTITY, "confirmacao_invalida")
        if int(prova.get("e", 0)) < time.time():
            return _recusa(status.HTTP_409_CONFLICT, "confirmacao_expirada")
        if pedido.frase.strip().upper() != _frase(int(prova["n"])):
            return _recusa(status.HTTP_422_UNPROCESSABLE_ENTITY, "frase_incorreta")
        total, digital = await _ativas(session, usuario.id)
        if (total, digital) != (prova["n"], prova["f"]):
            return _recusa(status.HTTP_409_CONFLICT, "confirmacao_desatualizada")
        if total == 0:
            return _recusa(status.HTTP_409_CONFLICT, "nada_a_apagar")
        chaves = list(
            (
                await session.execute(
                    text(
                        "UPDATE apostas SET selecionada = false WHERE usuario_id = :uid "
                        "AND selecionada AND chave IS NOT NULL RETURNING chave"
                    ),
                    {"uid": usuario.id},
                )
            ).scalars()
        )
        await _eventos(session, usuario.id, chaves, "APOSTA_CANCELADA", {"motivo": MOTIVO_APAGADA})
        # Como na exclusão individual, a revisão de uma aposta apagada sai da fila junto.
        await session.execute(
            text(
                "UPDATE revisao_pendente SET resolvido_em = now() WHERE usuario_id = :uid "
                "AND resolvido_em IS NULL AND NOT (motivo = ANY(CAST(:preservados AS text[]))) "
                "AND extracao_bruta->>'aposta_chave' = ANY(CAST(:chaves AS text[]))"
            ),
            {"uid": usuario.id, "chaves": chaves, "preservados": list(REVISOES_PRESERVADAS)},
        )
        lote_id = uuid4()
        criado_em = await session.scalar(text("SELECT clock_timestamp()"))
        resposta = {
            "lote_id": str(lote_id),
            "escopo": "apostas",
            "apostas_afetadas": len(chaves),
            "criado_em": criado_em.isoformat(),
            "restaurado_em": None,
            "apostas_restauradas": None,
        }
        linha = {
            "publico_id": lote_id,
            "operacao": "APAGAR",
            "status_code": 201,
            "resposta": resposta,
            "chaves": sorted(chaves),
        }
        return status.HTTP_201_CREATED, resposta, linha

    corpo = pedido.model_dump()
    return await _idempotente(session, usuario, idempotency_key, corpo, efeito)


@router.get(
    f"{PREFIXO}/intencao",
    response_model=IntencaoSaida,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "intencao_nao_encontrada: nenhum lote foi gravado com esta chave.",
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
    gravada = (
        await session.execute(
            select(models.ApostaLote).where(
                models.ApostaLote.usuario_id == usuario.id,
                models.ApostaLote.chave == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if gravada is None:
        return _recusa(status.HTTP_404_NOT_FOUND, "intencao_nao_encontrada")
    return JSONResponse(
        {
            "operacao": gravada.operacao,
            "status_code": gravada.status_code,
            "resposta": gravada.resposta,
        },
        headers=_privado(),
    )


async def _buscar(
    session: AsyncSession, usuario_id: int, lote_id: UUID
) -> tuple[models.ApostaLote | None, models.ApostaLote | None]:
    linhas = (
        await session.execute(
            select(models.ApostaLote).where(
                models.ApostaLote.usuario_id == usuario_id,
                (models.ApostaLote.publico_id == lote_id) | (models.ApostaLote.lote_id == lote_id),
            )
        )
    ).scalars()
    apagar = restaurar = None
    for linha in linhas:
        if linha.operacao == "APAGAR" and linha.publico_id == lote_id:
            apagar = linha
        elif linha.operacao == "RESTAURAR":
            restaurar = linha
    return apagar, restaurar


@router.get(
    f"{PREFIXO}/{{lote_id}}",
    response_model=LoteSaida,
    responses={404: {"model": ErrorResponse, "description": "lote_nao_encontrado."}},
)
async def consultar_lote(
    lote_id: UUID,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> JSONResponse:
    apagar, restaurar = await _buscar(session, usuario.id, lote_id)
    if apagar is None:
        return _recusa(status.HTTP_404_NOT_FOUND, "lote_nao_encontrado")
    return JSONResponse(_lote(apagar, restaurar), headers=_privado())


@router.post(
    f"{PREFIXO}/{{lote_id}}/restaurar",
    response_model=LoteSaida,
    responses={
        404: {"model": ErrorResponse, "description": "lote_nao_encontrado."},
        409: {
            "model": ErrorResponse,
            "description": "lote_restaurado, lote_em_curso ou idempotency_key_conflict.",
        },
        **VALIDACAO,
    },
)
async def restaurar_lote(
    lote_id: UUID,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    async def efeito() -> JSONResponse | tuple[int, dict[str, Any], dict[str, Any]]:
        apagar, restaurar = await _buscar(session, usuario.id, lote_id)
        if apagar is None:
            return _recusa(status.HTTP_404_NOT_FOUND, "lote_nao_encontrado")
        if restaurar is not None:
            return _recusa(status.HTTP_409_CONFLICT, "lote_restaurado")
        # Só o que este lote apagou e continua apagado: o que a pessoa restaurou ou apagou de
        # novo, uma a uma, depois do lote fica como ela deixou.
        chaves = list(
            (
                await session.execute(
                    text(
                        "UPDATE apostas SET selecionada = true WHERE usuario_id = :uid "
                        "AND NOT selecionada AND chave = ANY(CAST(:chaves AS text[])) "
                        "RETURNING chave"
                    ),
                    {"uid": usuario.id, "chaves": list(apagar.chaves or [])},
                )
            ).scalars()
        )
        await _eventos(session, usuario.id, chaves, "SELECAO_ALTERADA", {"selecionada": True})
        restaurado_em = await session.scalar(text("SELECT clock_timestamp()"))
        resposta = {
            **apagar.resposta,
            "restaurado_em": restaurado_em.isoformat(),
            "apostas_restauradas": len(chaves),
        }
        linha = {
            "publico_id": uuid4(),
            "operacao": "RESTAURAR",
            "lote_id": lote_id,
            "status_code": 200,
            "resposta": resposta,
        }
        return status.HTTP_200_OK, resposta, linha

    pedido = {"lote_id": str(lote_id)}
    return await _idempotente(session, usuario, idempotency_key, pedido, efeito)
