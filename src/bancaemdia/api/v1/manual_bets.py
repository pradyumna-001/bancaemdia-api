"""Manual creation and original-confirmation recovery, both scoped to the owner."""

import json
from typing import Annotated, get_args
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse
from bancaemdia.api.deps import get_current_user
from bancaemdia.api.v1.apostas import ApostaManual, _criar_manual
from bancaemdia.db.session import get_db
from bancaemdia.domain.access import AccountReadOnlyError
from bancaemdia.domain.manual_contract import (
    ManualAssignmentNotice,
    ManualCode,
    ManualCreationError,
    ManualCreationReceipt,
    ManualCreationRequest,
    ManualField,
    ManualOriginalConfirmation,
)
from bancaemdia.domain.registros import Usuario
from bancaemdia.models import RevisaoPendente
from bancaemdia.observability.metrics import apostas_created_total
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.services import manual_creation as service

PREFIX = "/api/v1/apostas-manuais/operacoes"
PRIVATE = {"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization"}
router = APIRouter(
    responses={
        **AUTHENTICATED_ERROR_RESPONSES,
        **{code: {"model": ManualCreationError} for code in (404, 409, 410, 422)},
        503: {
            "model": ManualCreationError | ErrorResponse,
            "description": "Dependência ou autenticação indisponível; resultado desconhecido, consultar intenção antes de nova ação.",
        },
    }
)


def failure(code: ManualCode, status: int, field: ManualField | None = None) -> JSONResponse:
    headers = dict(PRIVATE)
    if code == "manual_creation_busy":
        headers["Retry-After"] = "1"
    if status == 503:
        headers["Retry-After"] = "5"
    body = ManualCreationError(detail=code, campo=field)
    if code == "manual_receipt_expired":
        body.efeito = "confirmacao_anterior_retida"
    elif status == 503 or code == "manual_creation_busy":
        body.efeito = "resultado_desconhecido"
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=headers)


def validation_failure(error: RequestValidationError) -> JSONResponse:
    fields = get_args(ManualField)
    # The public field is an allowlisted identifier, never arbitrary JSON keys,
    # input/msg/ctx, customer text, database or provider diagnostics.
    field = next(
        (part for e in error.errors() for part in e.get("loc", ()) if part in fields), None
    )
    return failure("manual_validation_invalid", 422, field)


def database_failure(error: SQLAlchemyError) -> JSONResponse:
    if isinstance(error, DBAPIError):
        code = getattr(error.orig, "sqlstate", None)
        if code == "P0402":
            return JSONResponse({"detail": "account_read_only"}, status_code=402, headers=PRIVATE)
        if code == "22003":
            return failure("manual_validation_invalid", 422, "stake_unidades")
        if code in {"57014", "55P03", "40P01", "40001"}:
            return failure("manual_creation_busy", 409)
    return failure("manual_creation_unavailable", 503)


async def original_creation(
    request: ManualCreationRequest, user: Usuario, session: AsyncSession
) -> tuple[str, ManualOriginalConfirmation, list[ManualAssignmentNotice]]:
    body = request.model_dump()
    for name in ("conta_casa_id", "comissao_centavos"):
        body[name] = None if body[name] is None else int(body[name])
    result = await _criar_manual(ApostaManual.model_validate(body), user, session, confirmar=False)
    if result.status_code != 201:
        if result.status_code == 409:
            raise service.ManualRefusedError("manual_creation_busy", 409)
        raise service.ManualRefusedError("manual_account_invalid", 422, "conta_casa_id")
    data = json.loads(bytes(result.body))
    bet = data["aposta"]
    key = bet["chave"]
    events = await EventoRepo().list_by_aposta_chave(session, user.id, key)
    payload = next(e.payload_json for e in events if e.tipo == "APOSTA_CRIADA")
    notices = []
    if "aviso" in data:
        review = await session.scalar(
            select(RevisaoPendente).where(
                RevisaoPendente.usuario_id == user.id,
                RevisaoPendente.extracao_bruta["aposta_chave"].astext == key,
                RevisaoPendente.resolvido_em.is_(None),
            )
        )
        assert review is not None
        notices.append(
            ManualAssignmentNotice(
                codigo="conta_ambigua"
                if (review.extracao_bruta or {}).get("status") == "AMBIGUOUS"
                else "conta_ausente",
                revisao_id=str(review.id),
            )
        )
    original = ManualOriginalConfirmation(
        data_aposta=bet["data_aposta"],
        data_jogo=bet["data_jogo"],
        conta_casa_id=None if bet["conta_casa_id"] is None else str(bet["conta_casa_id"]),
        criterio_conta="multicontas_explicita"
        if request.conta_casa_id is not None
        else "data_jogo",
        valor_unidade_centavos=str(payload["valor_unidade_centavos"]),
        stake_centavos=str(bet["stake_centavos"]),
        valor_aposta_centavos=str(bet["valor_aposta_centavos"]),
        comissao_centavos=request.comissao_centavos or "0",
        freebet=bet["freebet"],
    )
    return key, original, notices


ID = Annotated[
    UUID, Path(description="UUID escolhido antes do POST, restrito ao usuário autenticado.")
]


@router.post(
    PREFIX + "/{intencao_id}",
    response_model=ManualCreationReceipt,
    status_code=201,
    responses={
        200: {
            "model": ManualCreationReceipt,
            "description": "Recibo original de repetição explícita do mesmo UUID/corpo.",
        }
    },
    summary="Criar aposta manual reconciliável",
    description="FULL_WRITE e CSRF atuais. Cria aposta, eventos, eventual revisão e recibo no mesmo commit. Mesmo UUID/corpo normalizado retorna o recibo sem recalcular defaults; outro corpo conflita. Nunca repetir automaticamente.",
)
async def create_manual(
    intencao_id: ID,
    request: ManualCreationRequest,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    async def create() -> tuple[str, ManualOriginalConfirmation, list[ManualAssignmentNotice]]:
        return await original_creation(request, usuario, session)

    try:
        receipt, created = await service.execute(session, usuario.id, intencao_id, request, create)
    except service.ManualRefusedError as error:
        await session.rollback()
        return failure(error.code, error.status, error.field)
    except AccountReadOnlyError:
        await session.rollback()
        raise
    except OverflowError:
        await session.rollback()
        return failure("manual_validation_invalid", 422, "stake_unidades")
    except SQLAlchemyError as error:
        await session.rollback()
        return database_failure(error)
    except Exception:
        # Unexpected creator errors must preserve the UUID recovery protocol,
        # without exposing diagnostics or claiming that an uncertain commit failed.
        await session.rollback()
        return failure("manual_creation_unavailable", 503)
    if created:
        apostas_created_total.labels(origem="manual", estado="PENDENTE").inc()
    return JSONResponse(
        receipt.model_dump(mode="json"), status_code=201 if created else 200, headers=PRIVATE
    )


@router.get(
    PREFIX + "/{intencao_id}",
    response_model=ManualCreationReceipt,
    summary="Recuperar confirmação original da aposta manual",
    description="READ_ONLY explícito; consulta o primário e o recibo durável por 90 dias, sem recalcular a aposta atual. 404/410 não provam rollback e não autorizam repetição automática. Identidade expirada permanece ocupada até apagamento autorizado da conta. Operação síncrona: nenhum estado assíncrono inventado.",
)
async def recover_manual(
    intencao_id: ID,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    try:
        receipt = await service.recover(session, usuario.id, intencao_id)
    except service.ManualRefusedError as error:
        return failure(error.code, error.status, error.field)
    except SQLAlchemyError as error:
        return database_failure(error)
    return JSONResponse(receipt.model_dump(mode="json"), headers=PRIVATE)
