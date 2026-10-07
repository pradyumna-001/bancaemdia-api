"""Site view: exact bet selection, live summary, daily graphs and aggregate XLSX."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user_snapshot
from bancaemdia.api.v1.painel import NOMES_DAS_ABAS
from bancaemdia.db.session import get_db_snapshot
from bancaemdia.domain.filtros_apostas import filtros_do_site
from bancaemdia.domain.painel import COLUNAS_EXPORTACAO, SecaoExportacao
from bancaemdia.domain.registros import Usuario
from bancaemdia.exportacao.painel_xlsx import (
    AbaXlsxAssincrona,
    gerar_painel_xlsx_assincrono,
    remover_arquivo_temporario,
)
from bancaemdia.repositories.painel_filtrado_repo import PainelFiltradoRepo

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)


class SaidaFiltrada(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricasFiltradas(SaidaFiltrada):
    total_apostas: int
    pendentes: int
    greens: int
    reds: int
    em_revisao: int
    anuladas: int
    resultados_desconhecidos: int
    giro_centavos: int
    base_roi_centavos: int
    retorno_centavos: int | None
    lucro_centavos: int | None
    freebets: int
    roi: str | None
    win_rate: str


class GrupoFiltrado(SaidaFiltrada):
    id: str | None
    nome: str | None
    metricas: MetricasFiltradas


class SnapshotFiltrado(SaidaFiltrada):
    fonte_dados: Literal["apostas_ao_vivo"] = "apostas_ao_vivo"
    criterio_temporal: Literal["data_aposta"] = "data_aposta"
    fuso_horario: Literal["America/Sao_Paulo"] = "America/Sao_Paulo"
    snapshot_em: datetime
    respondido_em: datetime


class PainelFiltradoSaida(SnapshotFiltrado):
    resumo: MetricasFiltradas
    por_casa: list[GrupoFiltrado]
    por_tipster: list[GrupoFiltrado]
    por_mercado: list[GrupoFiltrado]


class DatasetFiltrado(SaidaFiltrada):
    chave: Literal["lucro_centavos", "giro_centavos", "total_apostas"]
    unidade: Literal["centavos", "quantidade"]
    data: list[int | None]


class GraficosFiltradosSaida(SnapshotFiltrado):
    granularidade: Literal["dia"] = "dia"
    labels: list[str | None]
    datasets: list[DatasetFiltrado]
    total_periodo: MetricasFiltradas


def _privado(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie"


async def _snapshot(session: AsyncSession) -> dict[str, datetime]:
    instante = await session.scalar(select(func.transaction_timestamp()))
    assert isinstance(instante, datetime)
    return {"snapshot_em": instante, "respondido_em": datetime.now(UTC)}


@router.get("/api/v1/painel/filtrado", response_model=PainelFiltradoSaida)
async def consultar_painel_filtrado(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    filtros: Annotated[dict[str, object], Depends(filtros_do_site)],
    fresh: bool = False,
) -> PainelFiltradoSaida:
    _ = fresh  # Routing chooses primary; this view never refreshes or reads MVs.
    _privado(response)
    repo = PainelFiltradoRepo()
    return PainelFiltradoSaida.model_validate({
        "resumo": await repo.resumo(session, usuario.id, filtros),
        "por_casa": await repo.grupos(session, usuario.id, filtros, "casa"),
        "por_tipster": await repo.grupos(session, usuario.id, filtros, "tipster"),
        "por_mercado": await repo.grupos(session, usuario.id, filtros, "mercado"),
        **await _snapshot(session),
    })


@router.get("/api/v1/painel/filtrado/metricas", response_model=GraficosFiltradosSaida)
async def consultar_graficos_filtrados(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    filtros: Annotated[dict[str, object], Depends(filtros_do_site)],
    fresh: bool = False,
) -> GraficosFiltradosSaida:
    _ = fresh
    _privado(response)
    repo = PainelFiltradoRepo()
    dias = await repo.dias(session, usuario.id, filtros)
    return GraficosFiltradosSaida(
        labels=[
            None if d["periodo_inicio"] is None else d["periodo_inicio"].isoformat() for d in dias
        ],
        datasets=[
            DatasetFiltrado(chave=chave, unidade=unidade, data=[d[chave] for d in dias])
            for chave, unidade in [
                ("lucro_centavos", "centavos"),
                ("giro_centavos", "centavos"),
                ("total_apostas", "quantidade"),
            ]
        ],
        total_periodo=MetricasFiltradas.model_validate(
            await repo.resumo(session, usuario.id, filtros)
        ),
        **await _snapshot(session),
    )


async def _linhas(
    session: AsyncSession, usuario_id: int, filtros: dict[str, object], secao: SecaoExportacao
) -> AsyncIterator[tuple[object, ...]]:
    async for row in PainelFiltradoRepo().exportar(session, usuario_id, filtros, secao):
        yield tuple(row.get(coluna) for coluna in COLUNAS_EXPORTACAO[secao])


@router.get(
    "/api/v1/painel/filtrado/export",
    response_class=FileResponse,
    responses={
        200: {
            "description": "XLSX agregado da seleção filtrada, não uma lista de apostas.",
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def exportar_painel_filtrado(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    filtros: Annotated[dict[str, object], Depends(filtros_do_site)],
    fresh: bool = False,
) -> FileResponse:
    _ = fresh
    abas = [
        AbaXlsxAssincrona(
            nome=NOMES_DAS_ABAS[secao],
            cabecalhos=colunas,
            linhas=_linhas(session, usuario.id, filtros, secao),
        )
        for secao, colunas in COLUNAS_EXPORTACAO.items()
    ]
    arquivo = await gerar_painel_xlsx_assincrono(
        abas, nome_download=f"painel-filtrado-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.xlsx"
    )
    return FileResponse(
        arquivo.caminho,
        media_type=arquivo.media_type,
        filename=arquivo.nome_download,
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Authorization, Cookie",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(remover_arquivo_temporario, arquivo.caminho),
    )
