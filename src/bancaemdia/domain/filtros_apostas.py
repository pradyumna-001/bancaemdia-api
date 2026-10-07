"""One selection contract for bets and the site's filtered financial view."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from fastapi import HTTPException, Query
from pydantic import Field

from bancaemdia.domain.painel import FUSO_DO_BRASIL, PeriodoPainel, janela_do_periodo

BIGINT_MAX = 2**63 - 1
type IdFiltro = Annotated[
    Annotated[int, Field(ge=1, le=BIGINT_MAX)]
    | Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$", max_length=19)]
    | None,
    Query(),
]


class VisibilidadeApostas(StrEnum):
    ATIVAS = "ativas"
    APAGADAS = "apagadas"
    TODAS = "todas"


def resolver_filtros(
    *,
    periodo: PeriodoPainel | None = None,
    desde: datetime | None = None,
    ate: datetime | None = None,
    visibilidade: VisibilidadeApostas | None = None,
    incluir_apagadas: bool | None = None,
    **dimensoes: object,
) -> dict[str, object]:
    # Query IDs stay decimal strings in generated clients, avoiding JS rounding.
    # Numeric inputs remain accepted for legacy clients; SQL receives Python ints.
    for campo in (
        "casa_id",
        "tipster_id",
        "mercado_id",
        "competicao_id",
        "titular_id",
        "conta_casa_id",
        "grupo_id",
        "banca_id",
    ):
        valor = dimensoes.get(campo)
        if valor is not None:
            numero = int(valor) if isinstance(valor, (int, str)) else 0
            if not 1 <= numero <= BIGINT_MAX:
                raise HTTPException(422, f"{campo} precisa ser um BIGINT positivo")
            dimensoes[campo] = numero
    if periodo is not None and (desde is not None or ate is not None):
        raise HTTPException(422, "use periodo ou desde/ate, nunca ambos")
    # Old calls used naive timestamps. Preserve their UTC interpretation explicitly.
    desde = None if desde is None else desde.replace(tzinfo=UTC) if desde.tzinfo is None else desde
    ate = None if ate is None else ate.replace(tzinfo=UTC) if ate.tzinfo is None else ate
    if desde is not None and ate is not None and desde >= ate:
        raise HTTPException(422, "desde deve ser anterior a ate")
    if periodo is not None:
        janela = janela_do_periodo(periodo)
        desde = (
            None
            if janela.inicio is None
            else datetime.combine(janela.inicio, datetime.min.time(), FUSO_DO_BRASIL)
        )
        ate = datetime.combine(janela.fim, datetime.min.time(), FUSO_DO_BRASIL)
    legado = VisibilidadeApostas.TODAS if incluir_apagadas else VisibilidadeApostas.ATIVAS
    if visibilidade is not None and incluir_apagadas is not None and visibilidade != legado:
        raise HTTPException(422, "visibilidade e incluir_apagadas são conflitantes")
    origem = dimensoes.get("origem")
    if isinstance(origem, str) and (
        not origem or any(ord(c) < 32 or ord(c) == 127 for c in origem)
    ):
        raise HTTPException(422, "origem não pode ser vazia nem conter controles")
    return {
        **dimensoes,
        "desde": desde,
        "ate": ate,
        "visibilidade": visibilidade or legado,
        "incluir_apagadas": bool(incluir_apagadas),
    }


def filtros_do_site(
    periodo: Annotated[
        PeriodoPainel | None,
        Query(
            description="Janela civil de São Paulo; não combinar com desde/ate. Sem janela: todo o histórico."
        ),
    ] = None,
    desde: Annotated[
        datetime | None,
        Query(description="data_aposta inclusiva. ISO com offset; legado sem offset é UTC."),
    ] = None,
    ate: Annotated[
        datetime | None,
        Query(description="data_aposta exclusiva; datas ausentes não são incluídas em intervalos."),
    ] = None,
    casa_id: IdFiltro = None,
    tipster_id: IdFiltro = None,
    mercado_id: IdFiltro = None,
    competicao_id: IdFiltro = None,
    titular_id: IdFiltro = None,
    conta_casa_id: IdFiltro = None,
    grupo_id: IdFiltro = None,
    banca_id: IdFiltro = None,
    estado: Annotated[str | None, Query(max_length=64)] = None,
    origem: Annotated[str | None, Query(max_length=128)] = None,
    revisao_grave: bool | None = None,
    visibilidade: Annotated[
        VisibilidadeApostas | None,
        Query(
            description="ativas/apagadas/todas. Combinação conflitante com o legado retorna 422."
        ),
    ] = None,
    incluir_apagadas: Annotated[
        bool | None,
        Query(
            description="Legado: false=ativas, true=todas. Omitir ao usar visibilidade=apagadas."
        ),
    ] = None,
) -> dict[str, object]:
    return resolver_filtros(**locals())
