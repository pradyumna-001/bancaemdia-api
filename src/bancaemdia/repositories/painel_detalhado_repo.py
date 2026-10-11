"""Bounded daily and group-child reads of the same canonical site selection."""

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

from sqlalchemy import case, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.repositories.painel_filtrado_repo import PainelFiltradoRepo, _metricas, _saida

FUSO = "America/Sao_Paulo"


def metrics_columns(a: Any) -> list[Any]:
    settled = (
        a.estado.not_in(("PENDENTE", "ANULADA"))
        & a.revisao_grave.is_(False)
        & a.elegivel_financeiro
        & a.retorno_centavos.is_not(None)
    )
    return [
        *_metricas(a),
        func.count().filter(settled).label("liquidadas_conhecidas"),
        func.count().filter(~a.elegivel_financeiro).label("fontes_excluidas_financeiro"),
    ]


def detailed_metrics(row: Any) -> dict[str, Any]:
    raw = dict(row)
    result = _saida(raw)
    known_profit = int(raw["lucro_centavos"])
    known_return = int(raw["retorno_centavos"])
    incomplete = raw["pendentes"] + raw["em_revisao"] + raw["resultados_desconhecidos"]
    if raw["total_apostas"] == 0:
        status = "sem_atividade"
    elif not incomplete:
        status = "confirmado"
    elif raw["liquidadas_conhecidas"]:
        status = "parcial"
    elif raw["em_revisao"] or raw["resultados_desconhecidos"]:
        status = "desconhecido"
    else:
        status = "pendente"
    result.update(
        estado_dado=status,
        lucro_conhecido_centavos=str(known_profit),
        retorno_conhecido_centavos=str(known_return),
        lucro_centavos=None if incomplete else str(known_profit),
        retorno_centavos=None if incomplete else str(known_return),
        roi=None if incomplete else result["roi"],
    )
    for field in ("giro_centavos", "base_roi_centavos"):
        result[field] = str(int(result[field]))
    return result


def empty_metrics() -> dict[str, Any]:
    return detailed_metrics({
        **dict.fromkeys(
            (
                "total_apostas",
                "pendentes",
                "greens",
                "reds",
                "em_revisao",
                "anuladas",
                "resultados_desconhecidos",
                "freebets",
                "giro_centavos",
                "base_roi_centavos",
                "retorno_centavos",
                "lucro_centavos",
                "liquidadas_conhecidas",
                "fontes_excluidas_financeiro",
            ),
            0,
        ),
    })


class PainelDetalhadoRepo:
    def base(self, owner: int, filters: dict[str, object]) -> Any:
        return PainelFiltradoRepo()._base(owner, filters)

    async def stats(
        self, session: AsyncSession, owner: int, filters: dict[str, object]
    ) -> dict[str, Any]:
        a = self.base(owner, filters).c
        day = func.date(func.timezone(FUSO, a.data_aposta))
        row = (
            (
                await session.execute(
                    select(
                        *metrics_columns(a),
                        func.min(day).label("primeiro_dia"),
                        func.max(day).label("ultimo_dia"),
                    )
                )
            )
            .mappings()
            .one()
        )
        return {
            "metricas": detailed_metrics({
                k: v for k, v in row.items() if k not in {"primeiro_dia", "ultimo_dia"}
            }),
            "primeiro_dia": row["primeiro_dia"],
            "ultimo_dia": row["ultimo_dia"],
        }

    async def undated(
        self, session: AsyncSession, owner: int, filters: dict[str, object]
    ) -> dict[str, Any]:
        a = self.base(owner, filters).c
        row = (
            (await session.execute(select(*metrics_columns(a)).where(a.data_aposta.is_(None))))
            .mappings()
            .one()
        )
        return detailed_metrics(row)

    def daily_query(self, owner: int, filters: dict[str, object], start: date, end: date) -> Any:
        a = self.base(owner, filters).c
        day = func.date(func.timezone(FUSO, a.data_aposta))
        return (
            select(day.label("dia"), *metrics_columns(a))
            .where(day >= start, day < end)
            .group_by(day)
            .order_by(day)
        )

    async def days(
        self, session: AsyncSession, owner: int, filters: dict[str, object], start: date, end: date
    ) -> list[dict[str, Any]]:
        rows = (await session.execute(self.daily_query(owner, filters, start, end))).mappings()
        observed = {
            row["dia"]: detailed_metrics({k: v for k, v in row.items() if k != "dia"})
            for row in rows
        }
        return [
            {
                "dia": start + timedelta(days=i),
                "metricas": observed.get(start + timedelta(days=i), empty_metrics()),
            }
            for i in range((end - start).days)
        ]

    async def interval_metrics(
        self, session: AsyncSession, owner: int, filters: dict[str, object], start: date, end: date
    ) -> dict[str, Any]:
        a = self.base(owner, filters).c
        day = func.date(func.timezone(FUSO, a.data_aposta))
        row = (
            (await session.execute(select(*metrics_columns(a)).where(day >= start, day < end)))
            .mappings()
            .one()
        )
        return detailed_metrics(row)

    def attribution(self, owner: int, filters: dict[str, object]) -> Any:
        base = self.base(owner, filters)
        links, group, tipster = models.ApostaGrupo, models.GrupoAposta, models.Tipster
        memberships = (
            select(
                links.aposta_id,
                func.count().label("links"),
                func.count(group.id).label("validos"),
                func.min(group.id).label("id"),
            )
            .select_from(links)
            .outerjoin(group, (group.id == links.grupo_id) & (group.usuario_id == owner))
            .where(
                links.usuario_id == owner,
                links.aposta_id.in_(select(base.c.id)),
            )
            .group_by(links.aposta_id)
            .cte("vinculos_da_aposta")
        )
        code = case(
            (memberships.c.links.is_(None), 1),
            (memberships.c.links != memberships.c.validos, 3),
            (memberships.c.links > 1, 2),
            else_=0,
        )
        sole_id = case((code == 0, memberships.c.id), else_=None)
        joined = (
            base
            .outerjoin(memberships, memberships.c.aposta_id == base.c.id)
            .outerjoin(
                group,
                (group.id == sole_id) & (group.usuario_id == owner),
            )
            .outerjoin(tipster, tipster.id == base.c.tipster_id)
        )
        return (
            select(
                *base.c,
                code.label("atribuicao_codigo"),
                group.id.label("grupo_id"),
                group.nome.label("grupo_nome"),
                (~group.arquivado).label("grupo_ativo"),
                tipster.id.label("tipster_catalogo_id"),
                tipster.nome.label("tipster_nome"),
            )
            .select_from(joined)
            .cte("apostas_atribuidas")
        )

    def group_queries(self, owner: int, filters: dict[str, object]) -> tuple[Any, Any]:
        a = self.attribution(owner, filters).c
        group_fields = [a.atribuicao_codigo, a.grupo_id, a.grupo_nome, a.grupo_ativo]
        groups = (
            select(*group_fields, *metrics_columns(a)).group_by(*group_fields).cte("totais_grupos")
        )
        child_fields = [*group_fields, a.tipster_catalogo_id, a.tipster_nome]
        # A missing global catalog row is explicit rather than being named by an unrelated entity.
        child_status = case(
            (a.tipster_id.is_(None), "sem_tipster"),
            (a.tipster_catalogo_id.is_(None), "nao_resolvido"),
            else_="atribuido",
        )
        pairs = (
            select(*child_fields, child_status.label("tipster_atribuicao"), *metrics_columns(a))
            .group_by(*child_fields, child_status)
            .cte("totais_filhos")
        )
        return groups, pairs

    @staticmethod
    def order_key(pairs: Any) -> Any:
        return tuple_(
            pairs.c.atribuicao_codigo,
            func.coalesce(pairs.c.grupo_id, 0),
            func.coalesce(pairs.c.tipster_catalogo_id, 0),
            pairs.c.tipster_atribuicao,
        )

    async def hierarchy(
        self,
        session: AsyncSession,
        owner: int,
        filters: dict[str, object],
        limit: int,
        position: dict[str, Any] | None,
    ) -> dict[str, Any]:
        groups, pairs = self.group_queries(owner, filters)
        key = self.order_key(pairs)
        query = select(pairs)
        before = position is not None and position.get("direction") == "before"
        if position is not None:
            query = query.where(
                key < tuple_(*position["key"]) if before else key > tuple_(*position["key"])
            )
        query = query.order_by(key.desc() if before else key.asc()).limit(limit)
        rows = list((await session.execute(query)).mappings())
        if before:
            rows.reverse()
        ids = {(r["atribuicao_codigo"], r["grupo_id"]) for r in rows}
        # Group totals are calculated from the entire selection before child pagination.
        child_count = (
            select(func.count())
            .select_from(pairs)
            .where(
                pairs.c.atribuicao_codigo == groups.c.atribuicao_codigo,
                func.coalesce(pairs.c.grupo_id, 0) == func.coalesce(groups.c.grupo_id, 0),
            )
            .scalar_subquery()
            .label("total_filhos")
        )
        totals_query = select(groups, child_count).where(
            tuple_(groups.c.atribuicao_codigo, func.coalesce(groups.c.grupo_id, 0)).in_([
                (k, i or 0) for k, i in ids
            ])
        )
        totals = {
            (r["atribuicao_codigo"], r["grupo_id"]): r
            for r in (await session.execute(totals_query)).mappings()
        }
        sizes = (
            (
                await session.execute(
                    select(
                        select(func.count()).select_from(groups).scalar_subquery().label("grupos"),
                        select(func.count()).select_from(pairs).scalar_subquery().label("filhos"),
                    )
                )
            )
            .mappings()
            .one()
        )
        result: dict[tuple[int, int | None], dict[str, Any]] = {}
        metadata = {
            "atribuicao_codigo",
            "grupo_id",
            "grupo_nome",
            "grupo_ativo",
            "tipster_catalogo_id",
            "tipster_nome",
            "tipster_atribuicao",
            "total_filhos",
        }
        for row in rows:
            group_key = (row["atribuicao_codigo"], row["grupo_id"])
            if group_key not in result:
                total = totals[group_key]
                result[group_key] = {
                    "id": None if row["grupo_id"] is None else str(row["grupo_id"]),
                    "nome": row["grupo_nome"],
                    "ativo": row["grupo_ativo"],
                    "atribuicao": ("atribuida", "sem_grupo", "ambigua", "nao_resolvida")[
                        row["atribuicao_codigo"]
                    ],
                    "metricas": detailed_metrics({
                        k: v for k, v in total.items() if k not in metadata
                    }),
                    "tipsters": [],
                    "total_filhos": total["total_filhos"],
                }
            result[group_key]["tipsters"].append({
                "id": None
                if row["tipster_catalogo_id"] is None
                else str(row["tipster_catalogo_id"]),
                "nome": row["tipster_nome"],
                "atribuicao": row["tipster_atribuicao"],
                "metricas": detailed_metrics({k: v for k, v in row.items() if k not in metadata}),
            })

        def row_key(row: Any) -> list[Any]:
            return [
                row["atribuicao_codigo"],
                row["grupo_id"] or 0,
                row["tipster_catalogo_id"] or 0,
                row["tipster_atribuicao"],
            ]

        previous_key = following_key = None
        if rows:
            first, last = row_key(rows[0]), row_key(rows[-1])
            flags = (
                (
                    await session.execute(
                        select(
                            select(literal(1))
                            .select_from(pairs)
                            .where(key < tuple_(*first))
                            .exists()
                            .label("before"),
                            select(literal(1))
                            .select_from(pairs)
                            .where(key > tuple_(*last))
                            .exists()
                            .label("after"),
                        )
                    )
                )
                .mappings()
                .one()
            )
            previous_key = first if flags["before"] else None
            following_key = last if flags["after"] else None
        return {
            "grupos": list(result.values()),
            "total_grupos": sizes["grupos"],
            "total_filhos": sizes["filhos"],
            "previous_key": previous_key,
            "following_key": following_key,
        }

    async def export_hierarchy(
        self, session: AsyncSession, owner: int, filters: dict[str, object], children: bool
    ) -> AsyncIterator[dict[str, Any]]:
        groups, pairs = self.group_queries(owner, filters)
        source = pairs if children else groups
        query = select(source).order_by(source.c.atribuicao_codigo, source.c.grupo_id)
        if children:
            query = query.order_by(source.c.tipster_catalogo_id)
        stream = await session.stream(query)
        fields = {
            "atribuicao_codigo",
            "grupo_id",
            "grupo_nome",
            "grupo_ativo",
            "tipster_catalogo_id",
            "tipster_nome",
            "tipster_atribuicao",
        }
        try:
            async for row in stream.mappings():
                yield {
                    "grupo_id": None if row["grupo_id"] is None else str(row["grupo_id"]),
                    "grupo_nome": row["grupo_nome"],
                    "atribuicao": ("atribuida", "sem_grupo", "ambigua", "nao_resolvida")[
                        row["atribuicao_codigo"]
                    ],
                    "tipster_id": None
                    if not children or row["tipster_catalogo_id"] is None
                    else str(row["tipster_catalogo_id"]),
                    "tipster_nome": row.get("tipster_nome"),
                    **detailed_metrics({k: v for k, v in row.items() if k not in fields}),
                }
        finally:
            await stream.close()
