"""Explicit preview and application of an account holder switch."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user
from bancaemdia.db.session import get_db
from bancaemdia.domain.registros import Usuario
from bancaemdia.domain.titulares import (
    ChaveIdempotenciaEmConflitoError,
    TrocaInvalidaError,
    TrocaPedido,
    trocar_conta,
)

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)


class TrocaContaEntrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    casa_id: int = Field(strict=True, ge=1)
    conta_origem_id: int = Field(strict=True, ge=1)
    conta_destino_id: int = Field(strict=True, ge=1)
    efetiva_em: datetime
    estado_origem: Literal["DISPONIVEL", "LIMITADA", "ENCERRADA"]

    @field_validator("efetiva_em")
    @classmethod
    def exige_fuso(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("efetiva_em deve ter fuso horário")
        return value


class TrocaContaSaida(BaseModel):
    casa_id: int
    conta_origem_id: int
    conta_destino_id: int
    efetiva_em: datetime
    estado_origem: Literal["DISPONIVEL", "LIMITADA", "ENCERRADA"]
    apostas_afetadas_ids: list[int]
    aplicada: bool


async def _switch(
    entrada: TrocaContaEntrada,
    chave: str,
    usuario: Usuario,
    session: AsyncSession,
    *,
    aplicar: bool,
) -> TrocaContaSaida:
    pedido = TrocaPedido(
        casa_id=entrada.casa_id,
        conta_origem_id=entrada.conta_origem_id,
        conta_destino_id=entrada.conta_destino_id,
        efetiva_em=entrada.efetiva_em,
        estado_origem=entrada.estado_origem,
    )
    try:
        result = await trocar_conta(session, usuario.id, pedido, chave, aplicar=aplicar)
    except ChaveIdempotenciaEmConflitoError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TrocaInvalidaError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return TrocaContaSaida.model_validate(result)


@router.post(
    "/api/v1/titulares/trocas/preview",
    response_model=TrocaContaSaida,
    summary="Prévia de troca de conta da casa",
    description="Mostra IDs das apostas existentes que seriam afetadas por uma troca retroativa.",
)
async def preview_troca(
    entrada: TrocaContaEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    chave: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=160)],
) -> TrocaContaSaida:
    return await _switch(entrada, chave, usuario, session, aplicar=False)


@router.post(
    "/api/v1/titulares/trocas",
    response_model=TrocaContaSaida,
    status_code=status.HTTP_200_OK,
    summary="Trocar conta da casa",
    description="Fecha o uso atual e abre o destino no mesmo instante, sob travas transacionais.",
)
async def aplicar_troca(
    entrada: TrocaContaEntrada,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    chave: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=160)],
) -> TrocaContaSaida:
    return await _switch(entrada, chave, usuario, session, aplicar=True)
