"""Authorized original image delivery, including after review resolution."""

import asyncio
from typing import Annotated

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse
from bancaemdia.api.deps import get_current_user_primary_snapshot
from bancaemdia.config import get_settings
from bancaemdia.db.session import get_db_primary_snapshot
from bancaemdia.domain.access import require_read_operation
from bancaemdia.domain.foto_aposta import IMAGE_TYPES, InvalidPhotoError, validate_photo
from bancaemdia.domain.registros import Usuario
from bancaemdia.integrations.telegram.codec import _fernet
from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES
from bancaemdia.repositories.aposta_foto import ApostaFotoRepo
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.storage import midia_s3

PHOTO_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Authorization, Cookie",
}
NOT_FOUND = "não achei a foto desta aposta"
UNAVAILABLE = "não foi possível carregar a foto agora — tente novamente em instantes"
INVALID = "a foto armazenada é inválida, incompatível ou excede o limite permitido"
router = APIRouter(
    responses={code: spec for code, spec in AUTHENTICATED_ERROR_RESPONSES.items() if code != 402}
)


def photo_error(code: int, message: str) -> JSONResponse:
    headers = {**PHOTO_HEADERS, **({"Retry-After": "5"} if code == 503 else {})}
    return JSONResponse({"detail": message}, status_code=code, headers=headers)


@router.get(
    "/api/v1/apostas/{chave}/foto",
    response_class=Response,
    summary="Ler a foto original autorizada da aposta",
    description="Leitura por aposta e proveniência persistida, no primário, inclusive após revisão. FULL_WRITE e READ_ONLY. Sem extração, escrita, redirecionamento ou credenciais em URL. 404 representa aposta/foto inexistente, inacessível, ambígua ou já removida pela retenção.",
    responses={
        200: {
            "description": "Imagem original validada, até 20 MiB e 40 milhões de pixels; private, no-store.",
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in IMAGE_TYPES.values()
            },
            "headers": {
                name: {"schema": {"type": "string"}}
                for name in (*PHOTO_HEADERS, "Content-Disposition")
            },
        },
        401: {
            "model": ErrorResponse,
            "description": "Sessão necessária, inválida, expirada, revogada ou usuário inativo.",
        },
        429: {
            "model": ErrorResponse,
            "description": "Muitas requisições. Respeitar o intervalo da quota.",
            "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 1}}},
        },
        404: {"model": ErrorResponse, "description": NOT_FOUND},
        422: {"model": ErrorResponse, "description": INVALID},
        503: {
            "model": ErrorResponse,
            "description": UNAVAILABLE,
            "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 1}}},
        },
    },
)
async def ver_foto_aposta(
    chave: Annotated[
        str,
        Path(
            min_length=1,
            max_length=256,
            description="Chave da aposta acessível ao usuário autenticado; codificar como um segmento de URL.",
        ),
    ],
    usuario: Annotated[Usuario, Depends(get_current_user_primary_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_primary_snapshot)],
) -> Response:
    await require_read_operation(session, usuario.id, "aposta_foto")
    bet = await ApostaRepo().get_by_chave(session, usuario.id, chave)
    if bet is None:
        return photo_error(404, NOT_FOUND)
    photo = await ApostaFotoRepo().get(session, usuario.id, bet)
    if photo is None:
        return photo_error(404, NOT_FOUND)
    try:
        if photo.size > MAX_PHOTO_BYTES:
            raise InvalidPhotoError
        if photo.encrypted:
            if photo.content is None:
                raise InvalidPhotoError
            content = await asyncio.to_thread(_fernet().decrypt, photo.content)
        elif photo.s3_key is not None:
            bucket = get_settings().S3_UPLOAD_BUCKET
            if not bucket:
                raise midia_s3.PhotoStorageUnavailableError
            try:
                expected_key = midia_s3.key_for_hash(photo.hash)
            except ValueError:
                raise InvalidPhotoError from None
            if photo.s3_key != expected_key:
                raise InvalidPhotoError
            content = await midia_s3.download(bucket, photo.s3_key)
        elif photo.content is not None:
            content = photo.content
        else:
            raise InvalidPhotoError
        mime = await asyncio.to_thread(validate_photo, content, photo.mime, photo.hash)
    except midia_s3.PhotoObjectMissingError:
        return photo_error(404, NOT_FOUND)
    except (midia_s3.PhotoStorageUnavailableError, TimeoutError, InvalidToken):
        return photo_error(503, UNAVAILABLE)
    except (InvalidPhotoError, midia_s3.PhotoObjectInvalidError):
        return photo_error(422, INVALID)
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}[
        mime
    ]
    return Response(
        content,
        media_type=mime,
        headers={
            **PHOTO_HEADERS,
            "Content-Disposition": f'inline; filename="foto-aposta.{extension}"',
        },
    )
