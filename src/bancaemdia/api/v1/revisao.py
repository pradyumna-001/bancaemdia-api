from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, JsonValue, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    BetResponse,
    ValidationErrorResponse,
)
from bancaemdia.api.deps import get_current_user, get_current_user_snapshot
from bancaemdia.api.v1 import apostas as apostas_api
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db, get_db_snapshot
from bancaemdia.domain.materializar import projetar
from bancaemdia.domain.registros import RevisaoPendente, Usuario
from bancaemdia.domain.revisao_service import (
    ResolucaoInvalidaError,
    chave_da_aposta,
    evento_da_resolucao,
    planejar_resolucao,
)
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.mensagem_repo import MidiaArquivoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.storage.midia_s3 import signed_url
from bancaemdia.workers.pairing import confirmar_par

BIGINT_MAX = 2**63 - 1
TAMANHO_PADRAO_DA_PAGINA = 50
TAMANHO_MAXIMO_DA_PAGINA = 100
PAGINA_MAXIMA = BIGINT_MAX // TAMANHO_MAXIMO_DA_PAGINA
FUSO_DO_BRASIL = ZoneInfo("America/Sao_Paulo")
NAO_ACHEI = "não achei esta revisão"
NAO_ACHEI_FOTO = "não achei a foto desta revisão"
OCUPADA = "esta revisão está sendo resolvida agora — tente de novo em instantes"
JA_RESOLVIDA = "esta revisão já foi resolvida"
ORFA = "a aposta desta revisão não foi encontrada — nada foi alterado"
HISTORICO_INCOMPLETO = "o histórico desta aposta está incompleto — nada foi alterado"
TIPOS_DE_IMAGEM = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)


class CorrecaoDaRevisao(BaseModel):
    # O domínio mantém a allowlist: assim um campo desconhecido volta com o próprio nome no erro,
    # em vez de desaparecer silenciosamente durante a validação do corpo.
    model_config = ConfigDict(extra="allow")


class ResolucaoNova(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acao: Literal["CORRIGIR", "DESCARTAR", "MESMA"]
    aposta_corrigida: CorrecaoDaRevisao | None = None

    @field_validator("acao", mode="before")
    @classmethod
    def acao_em_maiusculas(cls, valor: object) -> object:
        return valor.strip().upper() if isinstance(valor, str) else valor


class ErroSaida(BaseModel):
    detail: str


class PaginacaoSaida(BaseModel):
    page: int
    page_size: int
    total: int


class RevisaoSaida(BaseModel):
    id: int
    midia_hash: str | None
    motivo: str
    extracao_bruta: dict[str, JsonValue] | None
    criado_em: datetime
    resolvido_em: datetime | None
    foto_url: str | None


class RevisoesPaginaSaida(BaseModel):
    data: list[RevisaoSaida]
    pagination: PaginacaoSaida


class EstatisticasSaida(BaseModel):
    total: int
    por_motivo: dict[str, int]
    mais_antiga_em: datetime | None
    idade_maxima_segundos: int


class RevisaoFechadaSaida(BaseModel):
    id: int
    resolvido_em: datetime


class ResolucaoSaida(BaseModel):
    revisao: RevisaoFechadaSaida
    acao: Literal["CORRIGIR", "DESCARTAR", "MESMA"]
    aposta: BetResponse
    eventos_gravados: int


def erro(status_code: int, mensagem: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": mensagem})


def _com_fuso(valor: datetime | None) -> datetime | None:
    if valor is None or valor.tzinfo is not None:
        return valor
    return valor.replace(tzinfo=FUSO_DO_BRASIL)


def _quando(valor: datetime | None) -> str | None:
    return None if valor is None else valor.isoformat()


def _foto_url(revisao: RevisaoPendente, storage: tuple[str, str] | None = None) -> str | None:
    if revisao.midia_hash is None or revisao.resolvido_em is not None:
        return None
    bucket = get_settings().S3_UPLOAD_BUCKET
    if bucket is not None and storage is not None:
        key, tipo = storage
        return signed_url(
            bucket, key, tipo if tipo in TIPOS_DE_IMAGEM else "application/octet-stream"
        )
    return f"/api/v1/revisao/{revisao.id}/foto"


def linha_da_revisao(
    revisao: RevisaoPendente, storage: tuple[str, str] | None = None
) -> dict[str, object]:
    return {
        "id": revisao.id,
        "midia_hash": revisao.midia_hash,
        "motivo": revisao.motivo,
        "extracao_bruta": revisao.extracao_bruta,
        "criado_em": _quando(revisao.criado_em),
        "resolvido_em": _quando(revisao.resolvido_em),
        "foto_url": _foto_url(revisao, storage),
    }


@router.get(
    "/api/v1/revisao/stats",
    response_model=EstatisticasSaida,
)
async def consultar_estatisticas(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
) -> JSONResponse:
    estatisticas = await RevisaoPendenteRepo().stats(session, usuario.id)
    return JSONResponse({
        "total": estatisticas.total,
        "por_motivo": estatisticas.por_motivo,
        "mais_antiga_em": _quando(estatisticas.mais_antiga_em),
        "idade_maxima_segundos": estatisticas.idade_maxima_segundos,
    })


@router.get(
    "/api/v1/revisao",
    response_model=RevisoesPaginaSaida,
)
async def listar_revisoes(
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    motivo: str | None = None,
    desde: datetime | None = None,
    ate: datetime | None = None,
    page: Annotated[int, Query(ge=1, le=PAGINA_MAXIMA)] = 1,
    page_size: Annotated[int, Query(ge=1, le=TAMANHO_MAXIMO_DA_PAGINA)] = TAMANHO_PADRAO_DA_PAGINA,
) -> JSONResponse:
    desde = _com_fuso(desde)
    ate = _com_fuso(ate)
    if desde is not None and ate is not None and desde >= ate:
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, "desde precisa ser anterior a ate")
    revisoes, total = await RevisaoPendenteRepo().list_page(
        session,
        usuario.id,
        {
            "motivo": motivo.strip() if motivo and motivo.strip() else None,
            "desde": desde,
            "ate": ate,
        },
        page,
        page_size,
    )
    storage: dict[str, tuple[str, str]] = {}
    if get_settings().S3_UPLOAD_BUCKET is not None:
        storage = await MidiaArquivoRepo().keys_for_hashes(
            session, [item.midia_hash for item in revisoes if item.midia_hash is not None]
        )
    return JSONResponse({
        "data": [
            linha_da_revisao(revisao, storage.get(revisao.midia_hash or "")) for revisao in revisoes
        ],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    })


@router.get(
    "/api/v1/revisao/{revisao_id}/foto",
    response_class=Response,
    responses={
        200: {
            "description": "Original review image.",
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}}
                for media_type in (*sorted(TIPOS_DE_IMAGEM), "application/octet-stream")
            },
        },
        404: {"model": ErroSaida, "description": "Revisão ou foto não encontrada"},
    },
)
async def ver_foto(
    revisao_id: Annotated[int, Path(ge=1, le=BIGINT_MAX)],
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    foto = await RevisaoPendenteRepo().get_foto_by_id(session, usuario.id, revisao_id)
    if foto is None:
        return erro(status.HTTP_404_NOT_FOUND, NAO_ACHEI_FOTO)
    tipo = foto.tipo if foto.tipo in TIPOS_DE_IMAGEM else "application/octet-stream"
    return Response(
        content=foto.conteudo,
        media_type=tipo,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/api/v1/revisao/{revisao_id}",
    response_model=RevisaoSaida,
    responses={404: {"model": ErroSaida, "description": "Revisão não encontrada"}},
)
async def ver_revisao(
    revisao_id: Annotated[int, Path(ge=1, le=BIGINT_MAX)],
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    revisao = await RevisaoPendenteRepo().get_by_id(session, usuario.id, revisao_id)
    if revisao is None:
        return erro(status.HTTP_404_NOT_FOUND, NAO_ACHEI)
    storage = None
    if revisao.midia_hash is not None and get_settings().S3_UPLOAD_BUCKET is not None:
        storage = (await MidiaArquivoRepo().keys_for_hashes(session, [revisao.midia_hash])).get(
            revisao.midia_hash
        )
    return JSONResponse(linha_da_revisao(revisao, storage))


@router.post(
    "/api/v1/revisao/{revisao_id}/resolver",
    response_model=ResolucaoSaida,
    responses={
        404: {"model": ErroSaida, "description": "Revisão não encontrada"},
        409: {"model": ErroSaida, "description": "Revisão ocupada ou inconsistente"},
        422: {
            "model": ErroSaida | ValidationErrorResponse,
            "description": "Resolução inválida",
        },
    },
)
async def resolver_revisao(
    revisao_id: Annotated[int, Path(ge=1, le=BIGINT_MAX)],
    pedido: ResolucaoNova,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    repositorio = RevisaoPendenteRepo()
    revisao = await repositorio.get_by_id(session, usuario.id, revisao_id)
    if revisao is None:
        await session.rollback()
        return erro(status.HTTP_404_NOT_FOUND, NAO_ACHEI)
    chave = chave_da_aposta(revisao.extracao_bruta)
    if chave is None:
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, ORFA)

    # A trava é a mesma do trabalhador e vem antes das duas linhas: uma releitura nunca passa por
    # cima da decisão humana, e duas resoluções não esperam o timeout de cinco segundos do primário.
    if not await apostas_api._travar(session, usuario.id, chave):
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, OCUPADA)
    travada = await repositorio.get_by_id_for_update(session, usuario.id, revisao_id)
    if travada is None:
        await session.rollback()
        return erro(status.HTTP_404_NOT_FOUND, NAO_ACHEI)
    if travada.resolvido_em is not None:
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, JA_RESOLVIDA)
    aposta_atual = await ApostaRepo().get_by_chave_for_update(session, usuario.id, chave)
    if aposta_atual is None:
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, ORFA)

    historico = await apostas_api._historico(session, usuario.id, chave)
    if not any(tipo == "APOSTA_CRIADA" for tipo, _, _ in historico):
        # A linha materializada não basta para reconstruir o log. Regravá-la a partir de um
        # histórico quebrado substituiria valores financeiros reais pelos defaults do projetor.
        await session.rollback()
        return erro(status.HTTP_409_CONFLICT, HISTORICO_INCOMPLETO)
    antes, _ = projetar(historico)
    if pedido.acao == "MESMA":
        candidata = (
            revisao.extracao_bruta.get("parceira_suspeita")
            if isinstance(revisao.extracao_bruta, dict)
            else None
        )
        if (
            pedido.aposta_corrigida is not None
            or not antes.get("duvida_de_par")
            or not isinstance(candidata, str)
        ):
            await session.rollback()
            return erro(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "esta revisão não tem um par para confirmar"
            )
        livre = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"pareador:{usuario.id}"},
        )
        if not livre:
            await session.rollback()
            return erro(status.HTTP_409_CONFLICT, OCUPADA)
        parceira = await ApostaRepo().get_by_chave_for_update(session, usuario.id, candidata)
        if parceira is None:
            await session.rollback()
            return erro(status.HTTP_409_CONFLICT, "a aposta candidata não existe mais")
        try:
            await confirmar_par(session, usuario.id, aposta_atual, parceira)
            await EventoRepo().append(
                session,
                {
                    "usuario_id": usuario.id,
                    "tipo": "REVISAO_RESOLVIDA",
                    "fonte": "manual",
                    "payload_json": evento_da_resolucao(
                        travada.id, "MESMA", travada.motivo, ()
                    ).payload,
                    "confianca": None,
                    "chat_id": None,
                    "message_id": None,
                    "aposta_chave": chave,
                },
            )
            resolvida = await repositorio.resolve(session, usuario.id, revisao_id)
            if resolvida is None:
                await session.rollback()
                return erro(status.HTTP_409_CONFLICT, JA_RESOLVIDA)
            depois_eventos = await apostas_api._historico(session, usuario.id, chave)
            depois, _ = projetar(depois_eventos)
            gravada = await ApostaRepo().get_by_chave(session, usuario.id, chave)
            assert gravada is not None
            await session.commit()
        except ValueError as recusa:
            await session.rollback()
            return erro(status.HTTP_409_CONFLICT, str(recusa))
        except Exception:
            await session.rollback()
            raise
        return JSONResponse({
            "revisao": {"id": resolvida.id, "resolvido_em": _quando(resolvida.resolvido_em)},
            "acao": "MESMA",
            "aposta": apostas_api.linha_da_aposta(gravada, depois),
            "eventos_gravados": len(depois_eventos) - len(historico),
        })
    correcoes = (
        None
        if pedido.aposta_corrigida is None
        else pedido.aposta_corrigida.model_dump(exclude_unset=True)
    )
    try:
        plano = planejar_resolucao(pedido.acao, correcoes, antes)
    except ResolucaoInvalidaError as recusa:
        await session.rollback()
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, str(recusa))

    recado = await apostas_api._conferir_ids(session, usuario.id, correcoes or {})
    if recado is not None:
        await session.rollback()
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, recado)
    novos = [
        *plano.eventos,
        evento_da_resolucao(travada.id, plano.acao, travada.motivo, plano.campos_corrigidos),
    ]
    depois, _ = projetar([*historico, *((e.tipo, e.fonte, e.payload) for e in novos)])

    try:
        gravada = await apostas_api._aplicar(session, usuario, chave, novos, depois)
        if gravada is None:
            await session.rollback()
            return erro(status.HTTP_409_CONFLICT, OCUPADA)
        resolvida = await repositorio.resolve(session, usuario.id, revisao_id)
        if resolvida is None:
            await session.rollback()
            return erro(status.HTTP_409_CONFLICT, JA_RESOLVIDA)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return JSONResponse({
        "revisao": {"id": resolvida.id, "resolvido_em": _quando(resolvida.resolvido_em)},
        "acao": plano.acao,
        "aposta": apostas_api.linha_da_aposta(gravada, depois),
        "eventos_gravados": len(novos),
    })
