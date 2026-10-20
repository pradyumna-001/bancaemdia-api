"""One multipart file per durable item; a SPA may submit multiple selected items."""

import asyncio
import hashlib
from io import BytesIO
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Path, Request, UploadFile
from kombu.exceptions import OperationalError
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import (
    AUTHENTICATED_ERROR_RESPONSES,
    BetResponse,
    ErrorResponse,
    OpenAPIResponses,
)
from bancaemdia.api.deps import get_current_user, get_current_user_primary_snapshot
from bancaemdia.api.v1.revisao import CorrecaoDaRevisao, RevisaoFechadaSaida
from bancaemdia.db.session import get_db, get_db_primary_snapshot
from bancaemdia.domain.foto_aposta import IMAGE_TYPES, InvalidPhotoError, validate_photo
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.web_print import (
    MAX_ATTEMPTS,
    PrintIntentResponse,
    PrintItemResponse,
    RetryPrint,
)
from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES
from bancaemdia.models.web_print import PrintAsset, PrintIntent, PrintItem
from bancaemdia.services.web_print import accept, find, lock, now, owner, reserve_attempt, response
from bancaemdia.workers.celery_app import app as celery

BASE = "/api/v1/prints/intencoes/{intent_id}"
ITEM = BASE + "/itens/{item_id}"
ERRORS: OpenAPIResponses = {
    **AUTHENTICATED_ERROR_RESPONSES,
    **{code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 410, 413)},
}
READ_ERRORS = {
    code: spec for code, spec in ERRORS.items() if code not in {402, 400, 403, 409, 410, 413}
}
router = APIRouter()


class PrintReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acao: Literal["CORRIGIR", "DESCARTAR"]
    aposta_corrigida: CorrecaoDaRevisao | None = None


class PrintReviewResponse(BaseModel):
    revisao: RevisaoFechadaSaida
    acao: Literal["CORRIGIR", "DESCARTAR"]
    aposta: BetResponse | None
    eventos_gravados: int


@router.post(
    "/api/v1/prints/revisoes/{revisao_id}/resolver",
    response_model=PrintReviewResponse,
    responses=ERRORS,
    summary="Resolver revisão de print ainda sem aposta financeira",
)
async def resolve_print_review(
    revisao_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    pedido: PrintReviewRequest,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PrintReviewResponse:
    from bancaemdia.repositories.aposta_lock import try_lock_bet
    from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
    from bancaemdia.services.web_print_review import resolve

    repo = RevisaoPendenteRepo()
    revision = await repo.get_by_id(session, usuario.id, revisao_id)
    if revision is None:
        raise HTTPException(404, "não achei esta revisão")
    raw = revision.extracao_bruta or {}
    key = raw.get("aposta_chave")
    if not raw.get("web_print_id") or not isinstance(key, str):
        raise HTTPException(409, "esta revisão já usa a resolução padrão")
    if not await try_lock_bet(session, usuario.id, key):
        raise HTTPException(409, "esta revisão está sendo resolvida")
    locked = await repo.get_by_id_for_update(session, usuario.id, revisao_id)
    if locked is None:
        raise HTTPException(404, "não achei esta revisão")
    if locked.resolvido_em is not None:
        raise HTTPException(409, "esta revisão já foi resolvida")
    result = await resolve(
        session,
        usuario,
        locked,
        pedido.acao,
        None
        if pedido.aposta_corrigida is None
        else pedido.aposta_corrigida.model_dump(exclude_unset=True),
    )
    await session.commit()
    return PrintReviewResponse.model_validate(result)


def decode(content: bytes, declared_mime: str | None) -> str:
    if not content:
        raise HTTPException(422, "o arquivo está vazio")
    try:
        with Image.open(BytesIO(content), formats=list(IMAGE_TYPES)) as image:
            mime = IMAGE_TYPES.get(image.format or "")
        if mime is None or (declared_mime not in {None, "application/octet-stream", mime}):
            raise InvalidPhotoError
        return validate_photo(content, mime, hashlib.sha256(content).hexdigest())
    except (
        InvalidPhotoError,
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ):
        raise HTTPException(
            422, "a imagem é inválida, incompatível ou excede o limite de pixels/frames"
        ) from None


async def publish(session: AsyncSession, uid: int, intent: UUID, item: UUID) -> None:
    await owner(session, uid)
    _, _, asset = await find(session, uid, intent, item)
    asset_id, state = asset.id, asset.status
    await session.rollback()
    if state != "accepted":
        return
    try:
        await asyncio.to_thread(
            celery.send_task,
            "materialization.web_print",
            kwargs={"usuario_id": uid, "asset_id": str(asset_id)},
            ignore_result=True,
        )
    except (OperationalError, ConnectionError, TimeoutError):
        # Admission is already durable. Beat republishes identifiers from this inbox.
        raise HTTPException(
            503,
            "a fila está indisponível; consulte esta identidade antes de qualquer nova ação",
            headers={"Retry-After": "5"},
        ) from None


@router.post(
    ITEM,
    status_code=202,
    response_model=PrintItemResponse,
    responses=ERRORS,
    summary="Receber um print web com identidade durável",
    description="Um arquivo raster por item, vários itens por intenção. Persistir os UUIDs antes do POST. Aceitação parcial por item; consultar GET após resposta perdida. Não repetir mutations automaticamente. 20 itens e 100 MiB por intenção; 20 MiB, 40 milhões de pixels e 256 frames por arquivo. Cookie+CSRF; novas escritas somente FULL_WRITE. Sem custo/consentimento no contrato.",
)
async def receive_print(
    request: Request,
    intent_id: UUID,
    item_id: UUID,
    file: Annotated[
        UploadFile, File(description="JPEG/PNG/GIF/WebP real; nome não é usado como localização")
    ],
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PrintItemResponse:
    form = await request.form()
    if len(form.multi_items()) != 1 or len(form.getlist("file")) != 1:
        await form.close()
        raise HTTPException(400, "envie exatamente um arquivo no campo file por item")
    await session.rollback()
    try:
        content = await file.read(MAX_PHOTO_BYTES + 1)
    finally:
        await file.close()
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "o arquivo passa de 20 MiB")
    mime = await asyncio.to_thread(decode, content, file.content_type)
    result = await accept(session, usuario.id, intent_id, item_id, content, mime)
    await session.commit()
    await publish(session, usuario.id, intent_id, item_id)
    return result


@router.get(
    ITEM,
    response_model=PrintItemResponse,
    responses=READ_ERRORS,
    summary="Reconciliar arquivo aceito sem conhecer ID devolvido pelo POST",
)
async def read_item(
    intent_id: UUID,
    item_id: UUID,
    usuario: Annotated[Usuario, Depends(get_current_user_primary_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_primary_snapshot)],
) -> PrintItemResponse:
    return response(*await find(session, usuario.id, intent_id, item_id))


@router.get(
    BASE,
    response_model=PrintIntentResponse,
    responses=READ_ERRORS,
    summary="Consultar todos os arquivos recebidos da intenção",
)
async def read_intent(
    intent_id: UUID,
    usuario: Annotated[Usuario, Depends(get_current_user_primary_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_primary_snapshot)],
) -> PrintIntentResponse:
    intent = await session.scalar(
        select(PrintIntent).where(
            PrintIntent.usuario_id == usuario.id,
            PrintIntent.id == intent_id,
            PrintIntent.expires_at > func.clock_timestamp(),
        )
    )
    if intent is None:
        raise HTTPException(404, "não achei esta intenção")
    rows = (
        await session.execute(
            select(PrintItem, PrintAsset)
            .join(
                PrintAsset,
                (PrintAsset.id == PrintItem.asset_id)
                & (PrintAsset.usuario_id == PrintItem.usuario_id),
            )
            .where(PrintItem.usuario_id == usuario.id, PrintItem.intent_id == intent_id)
            .order_by(PrintItem.criado_em, PrintItem.id)
        )
    ).all()
    items = [response(intent, item, asset) for item, asset in rows]
    terminal = sum(item.status in {"completed", "failed"} for item in items)
    failed = sum(item.status == "failed" for item in items)
    state = (
        "processing"
        if terminal < len(items)
        else "failed"
        if failed == len(items)
        else "partial"
        if failed
        else "complete"
    )
    return PrintIntentResponse.model_validate({
        "intent_id": intent_id,
        "state": state,
        "items": items,
        "received_items": len(items),
        "terminal_items": terminal,
        "reconciliation_expires_at": intent.expires_at,
    })


@router.post(
    ITEM + "/retry",
    status_code=202,
    response_model=PrintItemResponse,
    responses=ERRORS,
    summary="Tentar explicitamente somente um arquivo falho e elegível",
    description="Mesma identidade de item e bytes já aceitos. retry_id identifica a ação e expected_attempt evita repetição/stale action. Repetir o mesmo retry_id é uma consulta idempotente, não um novo processamento. Sucesso, aceito/processando, falha não retryable ou mídia expirada não podem ser tentados. FULL_WRITE requerido.",
)
async def retry_item(
    intent_id: UUID,
    item_id: UUID,
    pedido: RetryPrint,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PrintItemResponse:
    await session.rollback()
    await owner(session, usuario.id)
    await lock(session, usuario.id)
    intent, item, asset = await find(session, usuario.id, intent_id, item_id)
    if asset.retry_token == pedido.retry_id:
        if asset.attempt != pedido.expected_attempt + 1:
            raise HTTPException(409, "esta identidade de tentativa já foi usada com outra versão")
        return response(intent, item, asset)
    if (
        asset.attempt != pedido.expected_attempt
        or asset.status != "failed"
        or not asset.retryable
        or asset.attempt >= MAX_ATTEMPTS
        or asset.ciphertext is None
        or asset.media_expires_at <= now()
    ):
        raise HTTPException(409, "este arquivo não está elegível para tentativa")
    await reserve_attempt(session, usuario.id, asset)
    asset.attempt += 1
    asset.quota_day = now()
    asset.retry_token, asset.status, asset.error_code, asset.retryable = (
        pedido.retry_id,
        "accepted",
        None,
        False,
    )
    asset.lease_token, asset.lease_until = None, None
    result = response(intent, item, asset)
    await session.commit()
    await publish(session, usuario.id, intent_id, item_id)
    return result
