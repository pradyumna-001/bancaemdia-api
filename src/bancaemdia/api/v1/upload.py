import hmac
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from kombu.exceptions import OperationalError
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    ErrorResponse,
    UploadAcceptedResponse,
    UploadStatusResponse,
)
from bancaemdia.api.deps import get_current_user
from bancaemdia.config import get_settings
from bancaemdia.core.body_limits import UPLOAD_FORM_MARGIN
from bancaemdia.db.session import get_db
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.upload import (
    EXTENSAO_COMPACTADA,
    EXTENSAO_JSON,
    NOME_ARQUIVO_JSON,
    BilheteDoExport,
    ExportInvalidoError,
    ExportTelegram,
    bilhetes_do_export,
    estimar,
)
from bancaemdia.observability.tracing import custom_span, set_custom_span_attributes
from bancaemdia.repositories.upload_repo import UploadArquivoRepo, UploadBilheteRepo, UploadRepo
from bancaemdia.workers.celery_app import app as celery
from bancaemdia.workers.materialization import FUSO_DO_BRASIL

TAREFA = "materialization.processar_upload"
TAREFA_DE_FALHA = "materialization.falhar_upload"
INTERVALO_ENTRE_ENVIOS = timedelta(minutes=5)
# A margem cobre os cabeçalhos do formulário, que viajam junto com o arquivo no mesmo corpo.
MARGEM_DO_FORMULARIO = UPLOAD_FORM_MARGIN
CAMPOS_DO_FORMULARIO = 20
TAMANHO_DE_CAMPO = 64 * 1024
WEBHOOK_HEADER = "X-Webhook-Token"
WEBHOOK_PATH = "/webhook/upload-complete"

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)


class ConclusaoDoUpload(BaseModel):
    job_id: UUID
    status: str = Field(pattern="^(completed|failed)$")
    bets_processed: int = Field(ge=0)
    bets_failed: int = Field(ge=0)
    cost_usd: float = Field(ge=0)


def erro(status_code: int, mensagem: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": mensagem}, headers=headers)


def _inicio_do_dia() -> datetime:
    # O teto é por dia do Brasil: contado em UTC, ele zerava às 21h para quem estava postando
    # (defeito medido no projeto antigo).
    agora = datetime.now(FUSO_DO_BRASIL)
    return agora.replace(hour=0, minute=0, second=0, microsecond=0)


def _abrir(conteudo: bytes, nome: str) -> tuple[int, list[BilheteDoExport], int]:
    with custom_span("upload.parse", file_size=len(conteudo)) as span:
        with ExportTelegram(io.BytesIO(conteudo), nome) as export:
            mensagens = sum(1 for _ in export.mensagens())
            set_custom_span_attributes(span, "upload.parse", message_count=mensagens)
            return export.chat_id, bilhetes_do_export(export), mensagens


def _chats_pedidos(valores: list[str]) -> list[int] | None:
    pedidos: list[int] = []
    for valor in valores:
        for pedaco in str(valor).split(","):
            if pedaco.strip():
                pedidos.append(int(pedaco.strip()))
    return pedidos or None


async def _restante_do_dia(session: AsyncSession, usuario_id: int) -> int | None:
    limite = get_settings().UPLOAD_DAILY_PHOTO_LIMIT
    if not limite:
        return None
    ja_hoje = await UploadBilheteRepo().count_enviados_desde(session, usuario_id, _inicio_do_dia())
    return max(limite - ja_hoje, 0)


async def _esperar_intervalo(session: AsyncSession, usuario_id: int) -> int | None:
    ultimo = await UploadRepo().get_ultimo_aceito_em(session, usuario_id)
    if ultimo is None:
        return None
    faltam = (ultimo + INTERVALO_ENTRE_ENVIOS) - datetime.now(UTC)
    segundos = int(faltam.total_seconds()) + 1
    return segundos if segundos > 0 else None


def _enfileirar(upload_id: int, usuario_id: int) -> None:
    celery.send_task(
        TAREFA,
        kwargs={"upload_id": upload_id, "usuario_id": usuario_id},
        link_error=celery.signature(TAREFA_DE_FALHA, args=(upload_id, usuario_id), immutable=True),
    )


@router.post(
    "/api/v1/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UploadAcceptedResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Malformed multipart upload."},
        413: {"model": ErrorResponse, "description": "The uploaded export is too large."},
    },
)
async def receber_export(
    request: Request,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    # Os dois tetos vêm antes de ler o corpo: quem já passou deles não custa nem a memória do envio.
    segundos = await _esperar_intervalo(session, usuario.id)
    if segundos is not None:
        return erro(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"só dá para mandar um export a cada {INTERVALO_ENTRE_ENVIOS.seconds // 60} minutos —"
            f" tente de novo em {segundos} segundos",
            {"Retry-After": str(segundos)},
        )
    restante = await _restante_do_dia(session, usuario.id)
    if restante == 0:
        return erro(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"o teto de {get_settings().UPLOAD_DAILY_PHOTO_LIMIT} fotos por dia já foi usado —"
            " mande o mesmo arquivo amanhã e só o que faltou será lido",
        )

    # As duas conferências abriram transação: fechá-la antes de ler o corpo evita segurar uma
    # conexão do banco durante a subida de até 50 MB.
    await session.rollback()

    maximo = get_settings().UPLOAD_MAX_BYTES
    declarado = request.headers.get("content-length")
    # O teto do middleware é este mais a margem do formulário: entre um e outro, quem responde é
    # a rota, com recado em português em vez do texto cru do Starlette.
    if declarado and declarado.isdigit() and int(declarado) > maximo:
        return erro(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"o arquivo passa de {maximo // (1024 * 1024)} MB — exporte um período menor",
        )

    async with request.form(
        max_files=1, max_fields=CAMPOS_DO_FORMULARIO, max_part_size=TAMANHO_DE_CAMPO
    ) as formulario:
        arquivo = formulario.get("file")
        if not isinstance(arquivo, UploadFile):
            return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, "mande o export no campo `file`")
        nome = arquivo.filename or ""
        if not nome.lower().endswith((EXTENSAO_COMPACTADA, EXTENSAO_JSON)):
            return erro(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"mande o {EXTENSAO_COMPACTADA} da pasta que o Telegram Desktop gerou"
                f" (ou o {NOME_ARQUIVO_JSON} sozinho)",
            )
        try:
            chats = _chats_pedidos([str(v) for v in formulario.getlist("chat_filter")])
        except ValueError:
            return erro(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "`chat_filter` só aceita números de chat"
            )
        conteudo = await arquivo.read()

    if len(conteudo) > maximo:
        return erro(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"o arquivo passa de {maximo // (1024 * 1024)} MB — exporte um período menor",
        )
    try:
        chat_id, bilhetes, total_mensagens = await run_in_threadpool(_abrir, conteudo, nome)
    except ExportInvalidoError as recusa:
        return erro(status.HTTP_422_UNPROCESSABLE_ENTITY, str(recusa))
    if chats is not None and chat_id not in chats:
        return erro(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"este export é do chat {chat_id}, que não está no `chat_filter`",
        )

    estimativa = estimar(total_mensagens, bilhetes, restante)
    # A trava não espera: o trabalhador segura a mesma chave enquanto lê um export, e esperar por
    # ela estouraria o limite de 5 s do banco em cima do pedido de quem está enviando.
    livre = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:chave, 0))"),
        {"chave": f"upload:{usuario.id}"},
    )
    # A conferência do intervalo se repete aqui dentro: dois envios simultâneos passariam os dois
    # pela conferência de cima.
    if not livre or await _esperar_intervalo(session, usuario.id) is not None:
        await session.rollback()
        return erro(
            status.HTTP_429_TOO_MANY_REQUESTS, "outro export seu acabou de entrar — espere um pouco"
        )
    upload = await UploadRepo().create(
        session,
        {
            "usuario_id": usuario.id,
            "filename": nome,
            "chat_id": chat_id,
            "total_messages": estimativa.total_mensagens,
            "estimated_bets": estimativa.bilhetes,
            "estimated_cost_usd": Decimal(str(round(estimativa.custo_usd, 6))),
        },
    )
    await UploadArquivoRepo().create(session, usuario.id, upload.id, conteudo)
    await session.commit()

    try:
        await run_in_threadpool(_enfileirar, upload.id, usuario.id)
    except OperationalError:
        await UploadRepo().set_status(
            session, usuario.id, upload.id, "failed", "a fila estava fora do ar"
        )
        await UploadArquivoRepo().delete_by_upload_id(session, usuario.id, upload.id)
        await session.commit()
        return erro(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "não consegui pôr o seu export na fila agora — mande de novo em instantes",
        )

    corpo: dict[str, object] = {
        "job_id": str(upload.job_id),
        "estimated_bets": estimativa.bilhetes,
        "estimated_cost_usd": round(estimativa.custo_usd, 6),
        "status_url": f"/api/v1/upload/{upload.job_id}",
    }
    # "PRONTO · 0 apostas" sem explicação custou um dia ao dono do projeto antigo: quando nada vai
    # ser lido, a resposta diz por quê.
    if estimativa.bilhetes == 0:
        corpo["aviso"] = (
            "nenhuma foto veio neste export — no Telegram Desktop marque 'fotos' ao exportar"
        )
    elif estimativa.acima_do_teto:
        corpo["aviso"] = (
            f"{estimativa.acima_do_teto} fotos ficaram para depois do teto de hoje — mande o mesmo"
            " arquivo amanhã e só elas serão lidas"
        )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=corpo)


@router.get(
    "/api/v1/upload/{job_id}",
    response_model=UploadStatusResponse,
    responses={404: {"model": ErrorResponse, "description": "Upload job not found."}},
)
async def consultar_upload(
    job_id: UUID,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    upload = await UploadRepo().get_by_job_id(session, usuario.id, job_id)
    if upload is None:
        return erro(status.HTTP_404_NOT_FOUND, "não achei este envio")
    por_estado = await UploadBilheteRepo().count_by_estado(session, usuario.id, upload.id)
    total = sum(por_estado.values())
    lidos = por_estado.get("LIDO", 0) + por_estado.get("FALHOU", 0)
    # Envio recém-aceito ainda não tem bilhete nenhum: sem esta conta ele apareceria em 100%.
    percentual = (
        100 if upload.status == "completed" else (round(100 * lidos / total) if total else 0)
    )
    return JSONResponse({
        "job_id": str(upload.job_id),
        "status": upload.status,
        "filename": upload.filename,
        "total_messages": upload.total_messages,
        "estimated_bets": upload.estimated_bets,
        "estimated_cost_usd": float(upload.estimated_cost_usd),
        "progress": {
            "total": total,
            "pending": por_estado.get("PENDENTE", 0),
            "read": por_estado.get("LIDO", 0),
            "failed": por_estado.get("FALHOU", 0),
            "ignored": por_estado.get("IGNORADO", 0),
            "over_limit": por_estado.get("TETO", 0),
            "percent": percentual,
        },
        "bets_processed": upload.bets_processed,
        "bets_failed": upload.bets_failed,
        "cost_usd": float(upload.cost_usd),
        "erro": upload.erro,
        "criado_em": upload.criado_em.isoformat(),
        "concluido_em": None if upload.concluido_em is None else upload.concluido_em.isoformat(),
    })


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def upload_concluido(
    request: Request,
    conclusao: ConclusaoDoUpload,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    segredo = get_settings().UPLOAD_WEBHOOK_SECRET
    if not segredo:
        return erro(status.HTTP_503_SERVICE_UNAVAILABLE, "este servidor não tem segredo de webhook")
    if not hmac.compare_digest(request.headers.get(WEBHOOK_HEADER) or "", segredo):
        return erro(status.HTTP_403_FORBIDDEN, "segredo do webhook não confere")

    # O aviso chega sem usuário: a linha se abre pelo job_id, como a política do token da extensão.
    await session.execute(
        text("SELECT set_config('app.upload_job_id', :job, true)"),
        {"job": str(conclusao.job_id)},
    )
    fechado = await UploadRepo().finish_by_job_id(
        session,
        conclusao.job_id,
        conclusao.status,
        conclusao.bets_processed,
        conclusao.bets_failed,
        Decimal(str(round(conclusao.cost_usd, 6))),
    )
    if fechado is None:
        await session.rollback()
        return erro(status.HTTP_404_NOT_FOUND, "não achei este envio")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
