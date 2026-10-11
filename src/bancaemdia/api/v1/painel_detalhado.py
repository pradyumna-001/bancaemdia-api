"""Daily civil windows and source-group hierarchy, with server-owned reconciliation."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import get_current_user_snapshot
from bancaemdia.api.v1.painel_filtrado import SaidaFiltrada, SnapshotFiltrado, _privado, _snapshot
from bancaemdia.db.session import get_db_snapshot
from bancaemdia.domain.filtros_apostas import BIGINT_MAX, filtros_do_site
from bancaemdia.domain.painel import FUSO_DO_BRASIL
from bancaemdia.domain.painel_cursor import decode_cursor, encode_cursor
from bancaemdia.domain.registros import Usuario
from bancaemdia.exportacao.painel_xlsx import (
    AbaXlsxAssincrona,
    gerar_painel_xlsx_assincrono,
    remover_arquivo_temporario,
)
from bancaemdia.repositories.painel_detalhado_repo import PainelDetalhadoRepo

router = APIRouter(responses=AUTHENTICATED_ERROR_RESPONSES)
Money = Annotated[
    str,
    Field(
        pattern=r"^-?(0|[1-9][0-9]*)$",
        description="Centavos inteiros exatos em texto decimal; nunca float ou Number JavaScript.",
    ),
]
Identifier = Annotated[
    str,
    Field(
        pattern=r"^[1-9][0-9]{0,18}$",
        max_length=19,
        description="Identidade BIGINT exata, em texto decimal.",
    ),
]
Count = Annotated[int, Field(ge=0, json_schema_extra={"format": "int64"})]


class MetricasDetalhadas(SaidaFiltrada):
    total_apostas: Count
    pendentes: Count
    greens: Count
    reds: Count
    em_revisao: Count
    anuladas: Count
    resultados_desconhecidos: Count
    liquidadas_conhecidas: Count
    fontes_excluidas_financeiro: Count
    freebets: Count
    giro_centavos: Money
    base_roi_centavos: Money
    retorno_centavos: Money | None
    lucro_centavos: Money | None
    retorno_conhecido_centavos: Money
    lucro_conhecido_centavos: Money
    roi: str | None
    win_rate: str
    estado_dado: Literal["sem_atividade", "confirmado", "parcial", "pendente", "desconhecido"]


class ContextoDetalhado(SnapshotFiltrado):
    moeda: Literal["BRL"] = "BRL"
    escopo: Literal["apostas_legadas_brl"] = "apostas_legadas_brl"
    consistencia: Literal["snapshot_por_resposta"] = "snapshot_por_resposta"
    filtros: dict[str, str | bool | None]
    total_periodo: MetricasDetalhadas


class IntervaloDiario(SaidaFiltrada):
    inicio: date | None
    fim_exclusivo: date | None
    dias: Count


class DiaPainel(SaidaFiltrada):
    dia: date
    metricas: MetricasDetalhadas


class DiarioSaida(ContextoDetalhado):
    granularidade: Literal["dia"] = "dia"
    intervalo_total: IntervaloDiario
    intervalo_devolvido: IntervaloDiario
    subtotal_intervalo: MetricasDetalhadas
    sem_data: MetricasDetalhadas
    dias: list[DiaPainel]
    proximo_cursor: str | None
    anterior_cursor: str | None


class TipsterDoGrupo(SaidaFiltrada):
    id: Identifier | None
    nome: str | None
    atribuicao: Literal["atribuido", "sem_tipster", "nao_resolvido"]
    metricas: MetricasDetalhadas


class GrupoDeOrigem(SaidaFiltrada):
    id: Identifier | None
    nome: str | None
    ativo: bool | None
    atribuicao: Literal["atribuida", "sem_grupo", "ambigua", "nao_resolvida"]
    metricas: MetricasDetalhadas
    total_filhos: Count
    tipsters: list[TipsterDoGrupo]


class HierarquiaSaida(ContextoDetalhado):
    politica_atribuicao: Literal["vinculo_explicito_unico_da_aposta"] = (
        "vinculo_explicito_unico_da_aposta"
    )
    rotulos: Literal["catalogo_atual"] = "catalogo_atual"
    unidade_paginacao: Literal["grupo_tipster"] = "grupo_tipster"
    total_grupos: Count
    total_filhos: Count
    grupos: list[GrupoDeOrigem]
    proximo_cursor: str | None
    anterior_cursor: str | None


def filter_context(filters: dict[str, object]) -> dict[str, str | bool | None]:
    result: dict[str, str | bool | None] = {}
    for key, value in filters.items():
        result[key] = (
            value
            if value is None or isinstance(value, bool)
            else value.isoformat()
            if isinstance(value, datetime)
            else str(value)
        )
    return result


def validate_calendar(filters: dict[str, object]) -> None:
    # Reserve both calendar edge years for timezone conversion and exclusive ends.
    for field in ("desde", "ate"):
        value = filters.get(field)
        if isinstance(value, datetime) and not 2 <= value.year <= 9998:
            raise HTTPException(422, "limites diários devem estar entre os anos 2 e 9998")


def unavailable() -> HTTPException:
    return HTTPException(
        503,
        "projeção temporariamente indisponível",
        headers={"Cache-Control": "private, no-store", "Vary": "Authorization, Cookie"},
    )


def bounds(filters: dict[str, object], stats: dict[str, Any]) -> tuple[date | None, date | None]:
    try:
        return _bounds(filters, stats)
    except (OverflowError, ValueError):
        raise HTTPException(422, "intervalo fora dos limites do calendário diário") from None


def _bounds(filters: dict[str, object], stats: dict[str, Any]) -> tuple[date | None, date | None]:
    since, until = filters.get("desde"), filters.get("ate")
    start = (
        since.astimezone(FUSO_DO_BRASIL).date()
        if isinstance(since, datetime)
        else stats["primeiro_dia"]
    )
    if start is None:
        return None, None
    if isinstance(until, datetime):
        local = until.astimezone(FUSO_DO_BRASIL)
        end = local.date() + timedelta(days=local.time() != time.min)
    else:
        end = max(
            stats["ultimo_dia"] or start, datetime.now(FUSO_DO_BRASIL).date(), start
        ) + timedelta(days=1)
    return start, end


def window(start: date | None, end: date | None) -> dict[str, Any]:
    return {
        "inicio": start,
        "fim_exclusivo": end,
        "dias": 0 if start is None or end is None else (end - start).days,
    }


@router.get(
    "/api/v1/painel/filtrado/diario",
    response_model=DiarioSaida,
    summary="Consultar todos os dias do recorte financeiro em intervalos limitados",
    description="Sempre diário, inclusive 90d/1y/all. Cursor privado vinculado ao usuário e filtros; total completo e subtotal da janela separados. Fuso fixo America/Sao_Paulo, critério data_aposta, nunca data de liquidação.",
)
async def diario(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    filtros: Annotated[dict[str, object], Depends(filtros_do_site)],
    dias_por_pagina: Annotated[int, Query(ge=1, le=366)] = 90,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    inicio_dia: Annotated[
        date | None,
        Query(
            description="Janela civil opcional dentro do intervalo completo; não combinar com cursor."
        ),
    ] = None,
    fresh: bool = False,
) -> DiarioSaida:
    _ = fresh
    validate_calendar(filtros)
    _privado(response)
    if cursor is not None and inicio_dia is not None:
        raise HTTPException(422, "use cursor ou inicio_dia, nunca ambos")
    position = decode_cursor(cursor, usuario.id, "diario", filtros)
    repo = PainelDetalhadoRepo()
    try:
        stats = await repo.stats(session, usuario.id, filtros)
        start, end = bounds(filtros, stats)
        total = window(start, end)
        page_start = inicio_dia or start
        if position is not None:
            try:
                if (
                    position["range"] != [str(start), str(end)]
                    or position["size"] != dias_por_pagina
                ):
                    raise ValueError("changed window")
                page_start = date.fromisoformat(position["start"])
            except (ValueError, KeyError, TypeError):
                raise HTTPException(
                    422, "cursor inválido ou intervalo alterado; reinicie a paginação"
                ) from None
        if start is None or end is None:
            if cursor is not None or inicio_dia is not None:
                raise HTTPException(422, "histórico sem intervalo diário")
            from bancaemdia.repositories.painel_detalhado_repo import empty_metrics

            days, subtotal, returned, next_token, previous_token = (
                [],
                empty_metrics(),
                window(None, None),
                None,
                None,
            )
        else:
            if page_start is None or not start <= page_start < end:
                raise HTTPException(422, "inicio_dia fora do intervalo do recorte")
            page_end = page_start + timedelta(days=min((end - page_start).days, dias_por_pagina))
            days = await repo.days(session, usuario.id, filtros, page_start, page_end)
            subtotal = await repo.interval_metrics(
                session, usuario.id, filtros, page_start, page_end
            )
            returned = window(page_start, page_end)

            def token(day: date) -> str:
                return encode_cursor(
                    usuario.id,
                    "diario",
                    filtros,
                    {
                        "start": day.isoformat(),
                        "range": [str(start), str(end)],
                        "size": dias_por_pagina,
                    },
                )

            next_token = token(page_end) if page_end < end else None
            previous_token = (
                token(page_start - timedelta(days=min((page_start - start).days, dias_por_pagina)))
                if page_start > start
                else None
            )
        return DiarioSaida.model_validate({
            "filtros": filter_context(filtros),
            "total_periodo": stats["metricas"],
            "intervalo_total": total,
            "intervalo_devolvido": returned,
            "subtotal_intervalo": subtotal,
            "sem_data": await repo.undated(session, usuario.id, filtros),
            "dias": days,
            "proximo_cursor": next_token,
            "anterior_cursor": previous_token,
            **await _snapshot(session),
        })
    except DBAPIError:
        raise unavailable() from None


@router.get(
    "/api/v1/painel/filtrado/grupos",
    response_model=HierarquiaSaida,
    summary="Consultar lucro por grupo de origem e seus tipsters em lote",
    description="Uma aposta com vínculo único pertence a um grupo; múltiplos vínculos ficam em parcela ambígua, sem replicar finanças. A página limita pares grupo-tipster; os totais de cada grupo sempre abrangem todos os filhos do recorte.",
)
async def hierarquia(
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    filtros: Annotated[dict[str, object], Depends(filtros_do_site)],
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    fresh: bool = False,
) -> HierarquiaSaida:
    _ = fresh
    validate_calendar(filtros)
    _privado(response)
    position = decode_cursor(cursor, usuario.id, "grupos", filtros)
    if position is not None:
        key = position.get("key")
        if (
            not isinstance(key, list)
            or len(key) != 4
            or any(type(v) is not int or not 0 <= v <= BIGINT_MAX for v in key[:3])
            or not 0 <= key[0] <= 3
            or not isinstance(key[3], str)
            or key[3] not in {"atribuido", "sem_tipster", "nao_resolvido"}
            or position.get("direction") not in {"before", "after"}
        ):
            raise HTTPException(422, "cursor inválido")
    repo = PainelDetalhadoRepo()
    try:
        result = await repo.hierarchy(session, usuario.id, filtros, page_size, position)

        def token(key: list[Any] | None, direction: str) -> str | None:
            return (
                None
                if key is None
                else encode_cursor(
                    usuario.id, "grupos", filtros, {"key": key, "direction": direction}
                )
            )

        return HierarquiaSaida.model_validate({
            "filtros": filter_context(filtros),
            "total_periodo": (await repo.stats(session, usuario.id, filtros))["metricas"],
            "grupos": result["grupos"],
            "total_grupos": result["total_grupos"],
            "total_filhos": result["total_filhos"],
            "anterior_cursor": token(result["previous_key"], "before"),
            "proximo_cursor": token(result["following_key"], "after"),
            **await _snapshot(session),
        })
    except DBAPIError:
        raise unavailable() from None


EXPORT_METRICS = (
    "total_apostas",
    "pendentes",
    "em_revisao",
    "resultados_desconhecidos",
    "estado_dado",
    "giro_centavos",
    "base_roi_centavos",
    "retorno_centavos",
    "lucro_centavos",
    "retorno_conhecido_centavos",
    "lucro_conhecido_centavos",
    "freebets",
)
EXPORT_GROUPS = ("grupo_id", "grupo_nome", "atribuicao", *EXPORT_METRICS)
EXPORT_CHILDREN = (
    "grupo_id",
    "grupo_nome",
    "atribuicao",
    "tipster_id",
    "tipster_nome",
    *EXPORT_METRICS,
)
EXPORT_DAYS = ("dia", *EXPORT_METRICS)


async def export_rows(
    session: AsyncSession,
    owner: int,
    filters: dict[str, object],
    children: bool,
    columns: tuple[str, ...],
) -> AsyncIterator[tuple[object, ...]]:
    async for row in PainelDetalhadoRepo().export_hierarchy(session, owner, filters, children):
        yield tuple(row.get(c) for c in columns)


async def export_days(
    session: AsyncSession,
    owner: int,
    filters: dict[str, object],
    start: date | None,
    end: date | None,
) -> AsyncIterator[tuple[object, ...]]:
    repo = PainelDetalhadoRepo()
    if start is not None and end is not None:
        # Every SQL read remains bounded to a civil year, independent of history length.
        while start < end:
            stop = start + timedelta(days=min((end - start).days, 366))
            for row in await repo.days(session, owner, filters, start, stop):
                values = {"dia": row["dia"], **row["metricas"]}
                yield tuple(values.get(c) for c in EXPORT_DAYS)
            start = stop
    undated = await repo.undated(session, owner, filters)
    if undated["total_apostas"]:
        yield tuple({"dia": None, **undated}.get(c) for c in EXPORT_DAYS)


async def one_row(
    row: dict[str, Any], columns: tuple[str, ...]
) -> AsyncIterator[tuple[object, ...]]:
    yield await run_in_threadpool(lambda: tuple(row.get(c) for c in columns))


@router.get(
    "/api/v1/painel/filtrado/detalhado/export",
    response_class=FileResponse,
    responses={
        200: {
            "description": "XLSX da mesma população: resumo, grupos únicos, filhos e todos os dias; dinheiro e IDs como texto exato.",
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def exportar_detalhado(
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    filtros: Annotated[dict[str, object], Depends(filtros_do_site)],
    fresh: bool = False,
) -> FileResponse:
    _ = fresh
    validate_calendar(filtros)
    repo = PainelDetalhadoRepo()
    try:
        stats = await repo.stats(session, usuario.id, filtros)
        start, end = bounds(filtros, stats)
        if start is not None and end is not None and (end - start).days > 100000:
            raise HTTPException(422, "exportação limitada a 100000 dias; escolha desde/ate")
        _, pairs = repo.group_queries(usuario.id, filtros)
        from sqlalchemy import func, select

        count = await session.scalar(select(func.count()).select_from(pairs))
        if count is not None and count > 100000:
            raise HTTPException(422, "exportação limitada a 100000 pares; refine os filtros")
        snapshot = await _snapshot(session)
        context = {
            "criterio_temporal": "data_aposta",
            "fuso_horario": "America/Sao_Paulo",
            "fonte_dados": "apostas_ao_vivo",
            "moeda": "BRL",
            "snapshot_em": snapshot["snapshot_em"].isoformat(),
            **filter_context(filtros),
        }
        context_columns = tuple(context)
        abas = [
            AbaXlsxAssincrona(
                nome="Contexto",
                cabecalhos=context_columns,
                linhas=one_row(context, context_columns),
            ),
            AbaXlsxAssincrona(
                nome="Resumo",
                cabecalhos=EXPORT_METRICS,
                linhas=one_row(stats["metricas"], EXPORT_METRICS),
            ),
            AbaXlsxAssincrona(
                nome="Grupos",
                cabecalhos=EXPORT_GROUPS,
                linhas=export_rows(session, usuario.id, filtros, False, EXPORT_GROUPS),
            ),
            AbaXlsxAssincrona(
                nome="Tipsters por grupo",
                cabecalhos=EXPORT_CHILDREN,
                linhas=export_rows(session, usuario.id, filtros, True, EXPORT_CHILDREN),
            ),
            AbaXlsxAssincrona(
                nome="Diario",
                cabecalhos=EXPORT_DAYS,
                linhas=export_days(session, usuario.id, filtros, start, end),
            ),
        ]
        arquivo = await gerar_painel_xlsx_assincrono(
            abas, nome_download=f"painel-detalhado-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.xlsx"
        )
        return FileResponse(
            arquivo.caminho,
            media_type=arquivo.media_type,
            filename=arquivo.nome_download,
            headers={
                "Cache-Control": "private, no-store",
                "Vary": "Authorization, Cookie",
                "X-Content-Type-Options": "nosniff",
                "X-Snapshot-Em": snapshot["snapshot_em"].isoformat(),
            },
            background=BackgroundTask(remover_arquivo_temporario, arquivo.caminho),
        )
    except DBAPIError:
        raise unavailable() from None
