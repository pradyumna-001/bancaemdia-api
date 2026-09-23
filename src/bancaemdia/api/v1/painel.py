from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user_snapshot
from bancaemdia.db.session import get_db_snapshot
from bancaemdia.domain.painel import (
    COLUNAS_EXPORTACAO,
    FiltrosPainel,
    FrescorPainel,
    GrupoPainel,
    LinhaExportacao,
    MetricasGraficos,
    MetricasPainel,
    Painel,
    PeriodoPainel,
    PontoEvolucao,
    SaldoPainel,
    SecaoExportacao,
    formatar_decimal,
)
from bancaemdia.domain.registros import Usuario
from bancaemdia.exportacao.painel_xlsx import (
    AbaXlsxAssincrona,
    gerar_painel_xlsx_assincrono,
    remover_arquivo_temporario,
)
from bancaemdia.repositories.painel_repo import PainelRepo

BIGINT_MAX = 2**63 - 1
CACHE_PRIVADO = "private, no-store"
VARY_AUTENTICACAO = "Authorization, Cookie"

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)


class SaidaEstrita(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricasSaida(SaidaEstrita):
    total_apostas: int
    pendentes: int
    greens: int
    reds: int
    giro_centavos: int
    base_roi_centavos: int
    retorno_centavos: int
    lucro_centavos: int
    freebets: int
    roi: str
    win_rate: str
    roi_basis_points: int
    win_rate_basis_points: int


class SaldoSaida(SaidaEstrita):
    saldo_total_centavos: int | None
    saldo_conhecido_centavos: int
    contas_saldo_desconhecido: int
    escopo: Literal["contas_casa_all_time"]


class GrupoSaida(SaidaEstrita):
    id: int | None
    nome: str | None
    familia: str | None
    metricas: MetricasSaida


class EvolucaoSaida(SaidaEstrita):
    periodo_inicio: date
    banca_id: int | None
    banca_nome: str | None
    lucro_periodo_centavos: int
    lucro_acumulado_centavos: int
    saldo_centavos: int | None


class FrescorSaida(SaidaEstrita):
    atualizado_em: datetime | None
    idade_mv_segundos: str | None
    respondido_em: datetime
    replica_atraso_segundos: str | None
    replica_atraso_disponivel: bool
    replica_atraso_estado: Literal["disponivel", "primario_ou_sem_telemetria"]


class PainelSaida(FrescorSaida):
    resumo: MetricasSaida
    saldo: SaldoSaida
    por_casa: list[GrupoSaida]
    por_tipster: list[GrupoSaida]
    por_mercado: list[GrupoSaida]
    evolucao: list[EvolucaoSaida]


class DatasetSaida(SaidaEstrita):
    chave: str
    label: str
    unidade: str
    data: list[int]


class MetricasGraficosSaida(FrescorSaida):
    granularidade: Literal["dia", "semana", "mes"]
    labels: list[str]
    datasets: list[DatasetSaida]


def _aplicar_cache_privado(response: Response) -> None:
    response.headers["Cache-Control"] = CACHE_PRIVADO
    response.headers["Vary"] = VARY_AUTENTICACAO


def _metricas_saida(metricas: MetricasPainel) -> MetricasSaida:
    return MetricasSaida(
        total_apostas=metricas.total_apostas,
        pendentes=metricas.pendentes,
        greens=metricas.greens,
        reds=metricas.reds,
        giro_centavos=metricas.giro_centavos,
        base_roi_centavos=metricas.base_roi_centavos,
        retorno_centavos=metricas.retorno_centavos,
        lucro_centavos=metricas.lucro_centavos,
        freebets=metricas.freebets,
        roi=formatar_decimal(metricas.roi),
        win_rate=formatar_decimal(metricas.win_rate),
        roi_basis_points=metricas.roi_basis_points,
        win_rate_basis_points=metricas.win_rate_basis_points,
    )


def _saldo_saida(saldo: SaldoPainel) -> SaldoSaida:
    return SaldoSaida(
        saldo_total_centavos=saldo.saldo_total_centavos,
        saldo_conhecido_centavos=saldo.saldo_conhecido_centavos,
        contas_saldo_desconhecido=saldo.contas_saldo_desconhecido,
        escopo=saldo.escopo.value,
    )


def _grupo_saida(grupo: GrupoPainel) -> GrupoSaida:
    return GrupoSaida(
        id=grupo.id,
        nome=grupo.nome,
        familia=grupo.familia,
        metricas=_metricas_saida(grupo.metricas),
    )


def _evolucao_saida(ponto: PontoEvolucao) -> EvolucaoSaida:
    return EvolucaoSaida(
        periodo_inicio=ponto.periodo_inicio,
        banca_id=ponto.banca_id,
        banca_nome=ponto.banca_nome,
        lucro_periodo_centavos=ponto.contribuicao_centavos,
        lucro_acumulado_centavos=ponto.acumulado_centavos,
        saldo_centavos=ponto.saldo_centavos,
    )


def _frescor_saida(frescor: FrescorPainel) -> FrescorSaida:
    atraso_disponivel = frescor.replica_atraso_segundos is not None
    return FrescorSaida(
        atualizado_em=frescor.atualizado_em,
        idade_mv_segundos=(
            None
            if frescor.idade_mv_segundos is None
            else formatar_decimal(frescor.idade_mv_segundos, 3)
        ),
        respondido_em=frescor.respondido_em,
        replica_atraso_segundos=(
            None
            if frescor.replica_atraso_segundos is None
            else formatar_decimal(frescor.replica_atraso_segundos, 3)
        ),
        replica_atraso_disponivel=atraso_disponivel,
        replica_atraso_estado=("disponivel" if atraso_disponivel else "primario_ou_sem_telemetria"),
    )


def _painel_saida(painel: Painel) -> PainelSaida:
    frescor = _frescor_saida(painel.frescor)
    return PainelSaida(
        resumo=_metricas_saida(painel.resumo),
        saldo=_saldo_saida(painel.saldo),
        por_casa=[_grupo_saida(grupo) for grupo in painel.por_casa],
        por_tipster=[_grupo_saida(grupo) for grupo in painel.por_tipster],
        por_mercado=[_grupo_saida(grupo) for grupo in painel.por_mercado],
        evolucao=[_evolucao_saida(ponto) for ponto in painel.evolucao],
        atualizado_em=frescor.atualizado_em,
        idade_mv_segundos=frescor.idade_mv_segundos,
        respondido_em=frescor.respondido_em,
        replica_atraso_segundos=frescor.replica_atraso_segundos,
        replica_atraso_disponivel=frescor.replica_atraso_disponivel,
        replica_atraso_estado=frescor.replica_atraso_estado,
    )


def _graficos_saida(graficos: MetricasGraficos, frescor: FrescorPainel) -> MetricasGraficosSaida:
    saida_frescor = _frescor_saida(frescor)
    return MetricasGraficosSaida(
        granularidade=graficos.granularidade.value,
        labels=list(graficos.labels),
        datasets=[
            DatasetSaida(
                chave=serie.chave,
                label=serie.label,
                unidade=serie.unidade,
                data=list(serie.data),
            )
            for serie in graficos.datasets
        ],
        atualizado_em=saida_frescor.atualizado_em,
        idade_mv_segundos=saida_frescor.idade_mv_segundos,
        respondido_em=saida_frescor.respondido_em,
        replica_atraso_segundos=saida_frescor.replica_atraso_segundos,
        replica_atraso_disponivel=saida_frescor.replica_atraso_disponivel,
        replica_atraso_estado=saida_frescor.replica_atraso_estado,
    )


def _filtros(
    periodo: PeriodoPainel,
    casa_id: int | None,
    tipster_id: int | None,
    mercado_id: int | None,
) -> FiltrosPainel:
    return FiltrosPainel.criar(
        periodo,
        casa_id=casa_id,
        tipster_id=tipster_id,
        mercado_id=mercado_id,
    )


@router.get(
    "/api/v1/painel",
    response_model=PainelSaida,
    summary="Consultar o painel financeiro",
    description=(
        "Lê agregados materializados no snapshot da réplica. `fresh=true` escolhe o primário, "
        "mas não força nem simula um refresh das materialized views."
    ),
)
async def consultar_painel(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    periodo: Annotated[
        PeriodoPainel,
        Query(description="Janela civil no fuso America/Sao_Paulo"),
    ] = PeriodoPainel.TRINTA_DIAS,
    casa_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    tipster_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    mercado_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    fresh: Annotated[
        bool,
        Query(description="Lê as mesmas MVs no primário; não altera o horário do último refresh"),
    ] = False,
) -> PainelSaida:
    _ = fresh
    _aplicar_cache_privado(response)
    painel = await PainelRepo().consultar(
        session,
        usuario.id,
        _filtros(periodo, casa_id, tipster_id, mercado_id),
    )
    return _painel_saida(painel)


@router.get(
    "/api/v1/painel/metricas",
    response_model=MetricasGraficosSaida,
    summary="Consultar séries prontas para gráficos",
)
async def consultar_metricas(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    periodo: Annotated[
        PeriodoPainel,
        Query(description="Janela civil no fuso America/Sao_Paulo"),
    ] = PeriodoPainel.TRINTA_DIAS,
    casa_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    tipster_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    mercado_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
) -> MetricasGraficosSaida:
    _aplicar_cache_privado(response)
    filtros = _filtros(periodo, casa_id, tipster_id, mercado_id)
    repositorio = PainelRepo()
    graficos = await repositorio.consultar_metricas(session, usuario.id, filtros)
    frescor = await repositorio.frescor(session, usuario.id)
    return _graficos_saida(graficos, frescor)


def _valores_da_linha(
    linha: LinhaExportacao,
    colunas: Sequence[str],
) -> tuple[object, ...]:
    return tuple(linha.valores.get(coluna) for coluna in colunas)


async def _linhas_da_secao(
    repositorio: PainelRepo,
    session: AsyncSession,
    usuario_id: int,
    filtros: FiltrosPainel,
    secao: SecaoExportacao,
    colunas: Sequence[str],
) -> AsyncIterator[tuple[object, ...]]:
    async for linha in repositorio.iterar_exportacao(
        session,
        usuario_id,
        filtros,
        secoes=(secao,),
    ):
        yield _valores_da_linha(linha, colunas)


NOMES_DAS_ABAS: Mapping[SecaoExportacao, str] = {
    SecaoExportacao.RESUMO: "Resumo",
    SecaoExportacao.POR_CASA: "Por casa",
    SecaoExportacao.POR_TIPSTER: "Por tipster",
    SecaoExportacao.POR_MERCADO: "Por mercado",
    SecaoExportacao.POR_PERIODO: "Por periodo",
    SecaoExportacao.EVOLUCAO: "Evolucao",
}


@router.get(
    "/api/v1/painel/export",
    response_class=FileResponse,
    responses={
        200: {
            "description": "Streaming Excel workbook.",
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
    summary="Exportar o painel em Excel",
    description="Gera um XLSX write-only em disco e o envia em chunks, sem BytesIO integral.",
)
async def exportar_painel(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    periodo: Annotated[
        PeriodoPainel,
        Query(description="Janela civil no fuso America/Sao_Paulo"),
    ] = PeriodoPainel.TRINTA_DIAS,
    casa_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    tipster_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    mercado_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
) -> FileResponse:
    filtros = _filtros(periodo, casa_id, tipster_id, mercado_id)
    repositorio = PainelRepo()
    abas = [
        AbaXlsxAssincrona(
            nome=NOMES_DAS_ABAS[secao],
            cabecalhos=colunas,
            linhas=_linhas_da_secao(
                repositorio,
                session,
                usuario.id,
                filtros,
                secao,
                colunas,
            ),
        )
        for secao, colunas in COLUNAS_EXPORTACAO.items()
    ]
    agora = datetime.now(UTC)
    arquivo = await gerar_painel_xlsx_assincrono(
        abas,
        nome_download=f"painel-{agora:%Y%m%dT%H%M%SZ}.xlsx",
    )
    return FileResponse(
        arquivo.caminho,
        media_type=arquivo.media_type,
        filename=arquivo.nome_download,
        headers={
            "Cache-Control": CACHE_PRIVADO,
            "Vary": VARY_AUTENTICACAO,
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(remover_arquivo_temporario, arquivo.caminho),
    )
