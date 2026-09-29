"""Tenant-owned performance goals and analytics timezone preference."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user
from bancaemdia.db.session import get_db_primary
from bancaemdia.domain.painel import MetricasPainel, formatar_decimal
from bancaemdia.domain.registros import Usuario

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)
MetricaMeta = Literal["lucro_centavos", "giro_centavos", "roi", "win_rate", "total_apostas"]
StatusMeta = Literal["ativa", "concluida", "arquivada"]


class Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FusoEntrada(Estrito):
    fuso_horario: str = Field(min_length=1, max_length=64)

    @field_validator("fuso_horario")
    @classmethod
    def validar_fuso(cls, valor: str) -> str:
        try:
            ZoneInfo(valor)
        except (ZoneInfoNotFoundError, ValueError) as erro:
            raise ValueError("use um fuso IANA válido") from erro
        return valor


class MetaEntrada(Estrito):
    titulo: str = Field(min_length=1, max_length=120)
    metrica: MetricaMeta
    inicio: date
    fim: date
    alvo: Decimal
    linha_base: Decimal = Decimal(0)

    @model_validator(mode="after")
    def validar(self) -> MetaEntrada:
        if self.inicio > self.fim:
            raise ValueError("início deve ser anterior ou igual ao fim")
        if not self.alvo.is_finite() or not self.linha_base.is_finite():
            raise ValueError("alvo e linha_base precisam ser finitos")
        return self


class MetaAlteracao(Estrito):
    titulo: str | None = Field(default=None, min_length=1, max_length=120)
    metrica: MetricaMeta | None = None
    inicio: date | None = None
    fim: date | None = None
    alvo: Decimal | None = None
    linha_base: Decimal | None = None
    status: StatusMeta | None = None


class MetaSaida(Estrito):
    id: int
    titulo: str
    metrica: MetricaMeta
    inicio: date
    fim: date
    alvo: str
    linha_base: str
    status: StatusMeta
    valor_atual: str
    progresso: str | None
    alvo_atingido: bool


def _cache(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie"


def _validar_meta(meta: models.MetaDesempenho) -> None:
    if meta.inicio > meta.fim:
        raise HTTPException(422, "início deve ser anterior ou igual ao fim")
    if not meta.alvo.is_finite() or not meta.linha_base.is_finite():
        raise HTTPException(422, "alvo e linha_base precisam ser finitos")


async def _buscar(session: AsyncSession, usuario_id: int, meta_id: int) -> models.MetaDesempenho:
    meta = (
        await session.execute(
            select(models.MetaDesempenho).where(
                models.MetaDesempenho.id == meta_id,
                models.MetaDesempenho.usuario_id == usuario_id,
            )
        )
    ).scalar_one_or_none()
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "meta não encontrada")
    return meta


async def _progresso(session: AsyncSession, meta: models.MetaDesempenho) -> MetaSaida:
    linha = (
        (
            await session.execute(
                text(
                    """
                SELECT COALESCE(SUM(total_apostas), 0) AS total_apostas,
                       COALESCE(SUM(pendentes), 0) AS pendentes,
                       COALESCE(SUM(greens), 0) AS greens,
                       COALESCE(SUM(reds), 0) AS reds,
                       COALESCE(SUM(giro_centavos), 0) AS giro_centavos,
                       COALESCE(SUM(base_roi_centavos), 0) AS base_roi_centavos,
                       COALESCE(SUM(retorno_centavos), 0) AS retorno_centavos,
                       COALESCE(SUM(lucro_centavos), 0) AS lucro_centavos,
                       COALESCE(SUM(freebets), 0) AS freebets
                  FROM public.painel_analises_apostas
                 WHERE usuario_id = :usuario_id AND data >= :inicio AND data <= :fim
                """
                ),
                {"usuario_id": meta.usuario_id, "inicio": meta.inicio, "fim": meta.fim},
            )
        )
        .mappings()
        .one()
    )
    metricas = MetricasPainel.de_linha(cast(Mapping[str, object], linha))
    atual = Decimal(getattr(metricas, meta.metrica))
    distancia = meta.alvo - meta.linha_base
    progresso = None if distancia == 0 else (atual - meta.linha_base) / distancia
    atingido = atual >= meta.alvo if distancia >= 0 else atual <= meta.alvo
    return MetaSaida(
        id=meta.id,
        titulo=meta.titulo,
        metrica=meta.metrica,
        inicio=meta.inicio,
        fim=meta.fim,
        alvo=formatar_decimal(meta.alvo),
        linha_base=formatar_decimal(meta.linha_base),
        status=meta.status,
        valor_atual=formatar_decimal(atual),
        progresso=None if progresso is None else formatar_decimal(progresso),
        alvo_atingido=atingido,
    )


@router.get("/api/v1/painel/preferencias", response_model=FusoEntrada)
async def ler_preferencias(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user)],
) -> FusoEntrada:
    _cache(response)
    return FusoEntrada(fuso_horario=usuario.fuso_horario)


@router.patch("/api/v1/painel/preferencias", response_model=FusoEntrada)
async def alterar_preferencias(
    pedido: FusoEntrada,
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> FusoEntrada:
    _cache(response)
    await session.execute(
        update(models.Usuario)
        .where(models.Usuario.id == usuario.id)
        .values(fuso_horario=pedido.fuso_horario)
    )
    await session.commit()
    return pedido


@router.post("/api/v1/painel/metas", response_model=MetaSaida, status_code=201)
async def criar_meta(
    pedido: MetaEntrada,
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> MetaSaida:
    _cache(response)
    meta = models.MetaDesempenho(usuario_id=usuario.id, **pedido.model_dump())
    session.add(meta)
    await session.flush()
    saida = await _progresso(session, meta)
    await session.commit()
    return saida


@router.get("/api/v1/painel/metas", response_model=list[MetaSaida])
async def listar_metas(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> list[MetaSaida]:
    _cache(response)
    metas = (
        (
            await session.execute(
                select(models.MetaDesempenho)
                .where(models.MetaDesempenho.usuario_id == usuario.id)
                .order_by(models.MetaDesempenho.id)
            )
        )
        .scalars()
        .all()
    )
    return [await _progresso(session, meta) for meta in metas]


@router.get("/api/v1/painel/metas/{meta_id}", response_model=MetaSaida)
async def ler_meta(
    meta_id: int,
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> MetaSaida:
    _cache(response)
    return await _progresso(session, await _buscar(session, usuario.id, meta_id))


@router.patch("/api/v1/painel/metas/{meta_id}", response_model=MetaSaida)
async def alterar_meta(
    meta_id: int,
    pedido: MetaAlteracao,
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> MetaSaida:
    _cache(response)
    meta = await _buscar(session, usuario.id, meta_id)
    for campo, valor in pedido.model_dump(exclude_unset=True).items():
        if valor is None:
            raise HTTPException(422, f"{campo} não aceita null")
        setattr(meta, campo, valor)
    _validar_meta(meta)
    await session.flush()
    saida = await _progresso(session, meta)
    await session.commit()
    return saida


@router.delete("/api/v1/painel/metas/{meta_id}", response_model=MetaSaida)
async def arquivar_meta(
    meta_id: int,
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_primary)],
) -> MetaSaida:
    _cache(response)
    meta = await _buscar(session, usuario.id, meta_id)
    meta.status = "arquivada"
    await session.flush()
    saida = await _progresso(session, meta)
    await session.commit()
    return saida
