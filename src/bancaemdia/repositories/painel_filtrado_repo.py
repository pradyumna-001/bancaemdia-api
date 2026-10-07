"""Live aggregates of the exact selection used by the paginated bet list.

SQL does the grouping; only exact ratios are formatted by the existing panel domain.
Unknown financial results propagate to null instead of being treated as a zero return.
"""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.painel import SecaoExportacao, formatar_decimal, razao_exata
from bancaemdia.repositories.aposta_consolidacao import financial_predicate
from bancaemdia.repositories.selecao_apostas import selecionar_apostas


def _metricas(a: Any) -> list[Any]:
    liquidada = (
        a.estado.not_in(("PENDENTE", "ANULADA"))
        & a.revisao_grave.is_(False)
        & a.elegivel_financeiro
    )

    def soma(valor: Any) -> Any:
        return func.coalesce(func.sum(case((liquidada, valor), else_=0)), 0)

    return [
        func.count().label("total_apostas"),
        func.count().filter(a.estado == "PENDENTE").label("pendentes"),
        func.count().filter(liquidada & (a.estado == "GREEN")).label("greens"),
        func.count().filter(liquidada & (a.estado == "RED")).label("reds"),
        func.count().filter(a.revisao_grave).label("em_revisao"),
        func.count().filter(a.estado == "ANULADA").label("anuladas"),
        func
        .count()
        .filter(liquidada & a.retorno_centavos.is_(None))
        .label("resultados_desconhecidos"),
        func.count().filter(liquidada & a.freebet).label("freebets"),
        soma(a.stake_centavos).label("giro_centavos"),
        soma(case((a.freebet, a.valor_aposta_centavos), else_=a.stake_centavos)).label(
            "base_roi_centavos"
        ),
        soma(a.retorno_centavos).label("retorno_centavos"),
        soma(a.retorno_centavos - a.stake_centavos).label("lucro_centavos"),
    ]


def _saida(linha: Any) -> dict[str, Any]:
    result = dict(linha)
    unknown = int(result["resultados_desconhecidos"]) > 0
    result["roi"] = (
        None
        if unknown
        else formatar_decimal(
            razao_exata(int(result["lucro_centavos"]), int(result["base_roi_centavos"]))
        )
    )
    result["win_rate"] = formatar_decimal(
        razao_exata(int(result["greens"]), int(result["greens"]) + int(result["reds"]))
    )
    if unknown:
        result["retorno_centavos"] = None
        result["lucro_centavos"] = None
    return result


class PainelFiltradoRepo:
    def _base(self, usuario_id: int, filtros: dict[str, object]) -> Any:
        return (
            selecionar_apostas(usuario_id, filtros)
            .add_columns(financial_predicate().label("elegivel_financeiro"))
            .cte("apostas_selecionadas")
        )

    async def resumo(
        self, session: AsyncSession, usuario_id: int, filtros: dict[str, object]
    ) -> dict[str, Any]:
        a = self._base(usuario_id, filtros).c
        row = (await session.execute(select(*_metricas(a)))).mappings().one()
        return _saida(row)

    def _grupos(self, usuario_id: int, filtros: dict[str, object], eixo: str) -> Any:
        base = self._base(usuario_id, filtros)
        a = base.c
        entidade: Any
        if eixo == "casa":
            conta = models.ContaCasa
            entidade = models.Casa
            origem = base.outerjoin(
                conta, (conta.id == a.conta_casa_id) & (conta.usuario_id == usuario_id)
            ).outerjoin(entidade, entidade.id == conta.casa_id)
        elif eixo == "tipster":
            entidade = models.Tipster
            origem = base.outerjoin(entidade, entidade.id == a.tipster_id)
        elif eixo == "mercado":
            entidade = models.Mercado
            origem = base.outerjoin(entidade, entidade.id == a.mercado_id)
        else:
            raise ValueError("eixo não permitido")
        return (
            select(entidade.id.label("id"), entidade.nome.label("nome"), *_metricas(a))
            .select_from(origem)
            .group_by(entidade.id, entidade.nome)
            .order_by(entidade.id)
        )

    async def grupos(
        self, session: AsyncSession, usuario_id: int, filtros: dict[str, object], eixo: str
    ) -> list[dict[str, Any]]:
        result = await session.execute(self._grupos(usuario_id, filtros, eixo))
        return [
            {
                "id": None if row["id"] is None else str(row["id"]),
                "nome": row["nome"],
                "metricas": _saida({k: v for k, v in row.items() if k not in {"id", "nome"}}),
            }
            for row in result.mappings()
        ]

    def _dias(self, usuario_id: int, filtros: dict[str, object]) -> Any:
        a = self._base(usuario_id, filtros).c
        dia = func.date(func.timezone("America/Sao_Paulo", a.data_aposta))
        return (
            select(dia.label("periodo_inicio"), *_metricas(a))
            .group_by(dia)
            .order_by(dia.asc().nulls_last())
        )

    async def dias(
        self, session: AsyncSession, usuario_id: int, filtros: dict[str, object]
    ) -> list[dict[str, Any]]:
        result = await session.execute(self._dias(usuario_id, filtros))
        return [_saida(row) for row in result.mappings()]

    async def exportar(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: dict[str, object],
        secao: SecaoExportacao,
    ) -> AsyncIterator[dict[str, Any]]:
        if secao == SecaoExportacao.RESUMO:
            yield {
                **await self.resumo(session, usuario_id, filtros),
                "saldo_escopo": "nao_aplicavel_a_selecao_filtrada",
            }
            return
        if secao in {
            SecaoExportacao.POR_CASA,
            SecaoExportacao.POR_TIPSTER,
            SecaoExportacao.POR_MERCADO,
        }:
            eixo = {
                SecaoExportacao.POR_CASA: "casa",
                SecaoExportacao.POR_TIPSTER: "tipster",
                SecaoExportacao.POR_MERCADO: "mercado",
            }[secao]
            query = self._grupos(usuario_id, filtros, eixo)
        else:
            query = self._dias(usuario_id, filtros)
        result = await session.stream(query)
        acumulado: int | None = 0
        try:
            async for row in result.mappings():
                saida = _saida(row)
                saida["granularidade"] = "dia"
                if "id" in saida and saida["id"] is not None:
                    saida["id"] = str(saida["id"])
                if secao == SecaoExportacao.EVOLUCAO:
                    lucro = saida["lucro_centavos"]
                    acumulado = None if acumulado is None or lucro is None else acumulado + lucro
                    saida.update(contribuicao_centavos=lucro, acumulado_centavos=acumulado)
                yield saida
        finally:
            await result.close()
