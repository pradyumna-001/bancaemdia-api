"""Authenticated spreadsheet preparation, remapping and atomic confirmation."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse
from bancaemdia.api.deps import get_current_user
from bancaemdia.config import get_settings
from bancaemdia.core.body_limits import PLANILHA_MAX_BYTES
from bancaemdia.db.session import engine
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.sheet_contract import (
    ConfirmationView,
    ConfirmInput,
    FieldCatalog,
    PreviewPage,
    PreviewView,
    RemapInput,
    SourceInput,
    SourcePage,
    SourceView,
)
from bancaemdia.domain.sheet_xlsx import (
    FIELDS,
    MAX_CELLS,
    MAX_COLUMNS,
    MAX_INFLATED,
    MAX_ROWS,
    MAX_SECONDS,
    MAX_SHEETS,
    REQUIRED_FIELDS,
    SheetError,
    digest,
    inspect_xlsx,
)
from bancaemdia.domain.vocabulario import CASAS
from bancaemdia.models.sheet_import import SheetConfirmation, SheetPreview, SheetSource
from bancaemdia.services import sheet_import as service

PREFIX = "/api/v1/planilhas"
MAX_OPERATION_SECONDS = 30
router = APIRouter(
    prefix=PREFIX,
    tags=["Planilhas"],
    responses={
        **AUTHENTICATED_ERROR_RESPONSES,
        **{
            status: {"model": ErrorResponse, "description": "Recusa segura de importação"}
            for status in (400, 409, 410, 413, 422, 429, 503)
        },
    },
)


async def sheet_db(request: Request) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(
        engine.execution_options(isolation_level="SERIALIZABLE"), expire_on_commit=False
    )
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(request.state.usuario_id)},
        )
        yield session


User = Annotated[Usuario, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(sheet_db)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


async def run(session: AsyncSession, operation: Awaitable[Any]) -> Any:
    try:
        async with asyncio.timeout(MAX_OPERATION_SECONDS):
            result = await operation
            await session.commit()
        return result
    except TimeoutError:
        await session.rollback()
        return JSONResponse(
            {"detail": "import_unavailable"}, status_code=503, headers={"Retry-After": "5"}
        )
    except SheetError as error:
        await session.rollback()
        headers = (
            {"Retry-After": "86400"}
            if error.status == 429
            else {"Retry-After": "5"}
            if error.status == 503
            else {}
        )
        return JSONResponse({"detail": error.code}, status_code=error.status, headers=headers)
    except DBAPIError as error:
        await session.rollback()
        code = getattr(error.orig, "sqlstate", None)
        status = 409 if code in {"40001", "40P01"} else 402 if code == "P0402" else 503
        return JSONResponse(
            {
                "detail": "preview_obsolete"
                if status == 409
                else "account_read_only"
                if status == 402
                else "import_unavailable"
            },
            status_code=status,
            headers={"Retry-After": "5"} if status == 503 else {},
        )


@router.get("/campos", response_model=FieldCatalog)
async def fields(usuario: User) -> dict[str, Any]:
    labels = {
        "linha_id": "Identificador estável da linha",
        "casa": "Casa",
        "data_aposta": "Data da aposta",
        "data_jogo": "Data do jogo",
        "odd": "Odd",
        "stake_unidades": "Stake em unidades",
        "atualizada_em": "Atualização da fonte",
        "evento": "Evento",
        "descricao": "Descrição",
        "mercado_bruto": "Mercado",
        "freebet": "Freebet",
        "conta_casa_ref": "Conta explícita (multicontas)",
    }

    def kind(field: str) -> str:
        return (
            "date-time"
            if field in {"data_aposta", "data_jogo", "atualizada_em"}
            else "decimal"
            if field in {"odd", "stake_unidades"}
            else "boolean"
            if field == "freebet"
            else "account-id"
            if field == "conta_casa_ref"
            else "text"
        )

    return {
        "fields": [
            {
                "field": field,
                "label": labels[field],
                "type": kind(field),
                "required": field in REQUIRED_FIELDS,
                "options": [True, False]
                if field == "freebet"
                else sorted({name for name, _ in CASAS})
                if field == "casa"
                else [],
            }
            for field in FIELDS
        ],
        "formats": ["xlsx"],
        "max_bytes": PLANILHA_MAX_BYTES,
        "max_sheets": MAX_SHEETS,
        "max_rows": MAX_ROWS,
        "max_columns": MAX_COLUMNS,
        "max_cells": MAX_CELLS,
        "max_inflated_bytes": MAX_INFLATED,
        "max_expansion_ratio": 100,
        "max_parse_seconds": MAX_SECONDS,
        "max_operation_seconds": MAX_OPERATION_SECONDS,
        "max_cell_characters": 1000,
        "daily_previews": get_settings().SHEET_DAILY_PREVIEW_LIMIT,
    }


@router.put("/fontes/{source_id}", response_model=SourceView)
async def create_source(source_id: UUID, body: SourceInput, usuario: User, session: DB) -> Any:
    async def operation() -> dict[str, Any]:
        await service.lock(session, usuario.id)
        existing = await session.get(SheetSource, (usuario.id, source_id))
        if existing is not None:
            if existing.nome != body.nome:
                raise SheetError("identity_conflict", 409)
            return service.source_view(existing)
        count = await session.scalar(
            select(func.count())
            .select_from(SheetSource)
            .where(SheetSource.usuario_id == usuario.id)
        )
        if int(count or 0) >= 1000:
            raise SheetError("source_limit", 413)
        record = SheetSource(usuario_id=usuario.id, id=source_id, nome=body.nome, version=0)
        session.add(record)
        await session.flush()
        return service.source_view(record)

    return await run(session, operation())


@router.get("/fontes", response_model=SourcePage)
async def list_sources(usuario: User, session: DB, page: Page = 1, page_size: PageSize = 50) -> Any:
    async def operation() -> dict[str, Any]:
        query = select(SheetSource).where(SheetSource.usuario_id == usuario.id)
        total = await session.scalar(
            select(func.count())
            .select_from(SheetSource)
            .where(SheetSource.usuario_id == usuario.id)
        )
        records = (
            await session.scalars(
                query
                .order_by(SheetSource.created_at, SheetSource.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return {
            "data": [service.source_view(source) for source in records],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    return await run(session, operation())


@router.get("/fontes/{source_id}", response_model=SourceView)
async def get_source(source_id: UUID, usuario: User, session: DB) -> Any:
    async def operation() -> dict[str, Any]:
        return service.source_view(await service.source_get(session, usuario.id, source_id))

    return await run(session, operation())


@router.post(
    "/fontes/{source_id}/previas/{preview_id}", response_model=PreviewView, status_code=201
)
async def upload_preview(
    source_id: UUID,
    preview_id: UUID,
    request: Request,
    arquivo: Annotated[
        UploadFile, File(description="XLSX real com linha_id estável; nunca usado como caminho")
    ],
    usuario: User,
    session: DB,
) -> Any:
    async def operation() -> dict[str, Any]:
        form = await request.form()
        if len(form.multi_items()) != 1:
            raise SheetError("invalid_multipart", 400)
        content = await arquivo.read(PLANILHA_MAX_BYTES + 1)
        await arquivo.close()
        file_hash = hashlib.sha256(content).hexdigest()
        workbook = await asyncio.to_thread(inspect_xlsx, content)
        await service.lock(session, usuario.id)
        source = await service.source_get(session, usuario.id, source_id)
        current_snapshot = await service.fingerprint(session, usuario.id)
        preview = await service.save_preview(
            session,
            usuario.id,
            source,
            preview_id,
            digest([source_id, file_hash]),
            file_hash,
            workbook,
            current_snapshot=current_snapshot,
        )
        return await service.preview_view(session, preview, current_snapshot=current_snapshot)

    return await run(session, operation())


@router.get("/previas/{preview_id}", response_model=PreviewView)
async def get_preview(preview_id: UUID, usuario: User, session: DB) -> Any:
    async def operation() -> dict[str, Any]:
        return await service.preview_view(
            session, await service.preview_get(session, usuario.id, preview_id)
        )

    return await run(session, operation())


@router.get("/previas/{preview_id}/linhas", response_model=PreviewPage)
async def preview_rows(
    preview_id: UUID, usuario: User, session: DB, page: Page = 1, page_size: PageSize = 50
) -> Any:
    async def operation() -> dict[str, Any]:
        preview = await service.preview_get(session, usuario.id, preview_id)
        rows = service.unseal(preview.private_data)["plan"]["rows"]
        return {
            "data": [
                {key: value for key, value in row.items() if not key.startswith("internal_")}
                for row in rows[(page - 1) * page_size : page * page_size]
            ],
            "total": len(rows),
            "page": page,
            "page_size": page_size,
        }

    return await run(session, operation())


@router.post(
    "/previas/{preview_id}/remapear/{new_preview_id}", response_model=PreviewView, status_code=201
)
async def remap(
    preview_id: UUID, new_preview_id: UUID, body: RemapInput, usuario: User, session: DB
) -> Any:
    async def operation() -> dict[str, Any]:
        await service.lock(session, usuario.id)
        previous = await service.preview_get(session, usuario.id, preview_id)
        source = await service.source_get(session, usuario.id, previous.source_id)
        request_hash = digest([preview_id, body.model_dump()])
        existing = await session.get(SheetPreview, (usuario.id, new_preview_id))
        if existing is not None:
            if existing.request_hash != request_hash or existing.source_id != source.id:
                raise SheetError("identity_conflict", 409)
            return await service.preview_view(
                session, await service.preview_get(session, usuario.id, new_preview_id)
            )
        if previous.revision != body.expected_revision or source.current_preview != previous.id:
            raise SheetError("preview_obsolete", 409)
        workbook = service.unseal(previous.private_data)["workbook"]
        current_snapshot = await service.fingerprint(session, usuario.id)
        preview = await service.save_preview(
            session,
            usuario.id,
            source,
            new_preview_id,
            request_hash,
            previous.file_hash,
            workbook,
            body.sheet,
            body.mapping,
            previous.expires_at,
            current_snapshot=current_snapshot,
        )
        return await service.preview_view(session, preview, current_snapshot=current_snapshot)

    return await run(session, operation())


@router.post("/confirmacoes/{confirmation_id}", response_model=ConfirmationView)
async def apply_preview(
    confirmation_id: UUID, body: ConfirmInput, usuario: User, session: DB
) -> Any:
    return await run(
        session,
        service.confirm(session, usuario, confirmation_id, body.preview_id, body.preview_hash),
    )


@router.get("/confirmacoes/{confirmation_id}", response_model=ConfirmationView)
async def get_confirmation(confirmation_id: UUID, usuario: User, session: DB) -> Any:
    async def operation() -> dict[str, Any]:
        record = await session.get(SheetConfirmation, (usuario.id, confirmation_id))
        if record is None:
            raise SheetError("import_not_found", 404)
        if record.expires_at <= service.now():
            raise SheetError("import_expired", 410)
        return service.unseal(record.private_result)

    return await run(session, operation())
