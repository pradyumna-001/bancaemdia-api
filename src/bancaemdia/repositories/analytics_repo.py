"""Read the live extension of the dashboard's canonical per-bet view."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy import bindparam, column, literal_column, select, table
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.analytics import analisar_apostas
from bancaemdia.domain.painel import FiltrosPainel


class AnalyticsRepo:
    async def consultar(
        self, session: AsyncSession, usuario_id: int, filtros: FiltrosPainel, fuso_horario: str
    ) -> dict[str, object]:
        source = table(
            "painel_analises_apostas",
            column("usuario_id"),
            column("data"),
            column("casa_id"),
            column("tipster_id"),
            column("mercado_id"),
            schema="public",
        )
        condicoes = [
            source.c.usuario_id == bindparam("usuario_id"),
            source.c.data < bindparam("fim"),
        ]
        parametros: dict[str, object] = {
            "usuario_id": usuario_id,
            "fim": filtros.janela.fim,
        }
        if filtros.janela.inicio is not None:
            condicoes.append(source.c.data >= bindparam("inicio"))
            parametros["inicio"] = filtros.janela.inicio
        for coluna in ("casa_id", "tipster_id", "mercado_id"):
            valor = getattr(filtros, coluna)
            if valor is not None:
                condicoes.append(source.c[coluna] == bindparam(coluna))
                parametros[coluna] = valor
        resultado: Result[tuple[object, ...]] = await session.execute(
            select(literal_column("*")).select_from(source).where(*condicoes),
            parametros,
        )
        linhas = [cast(Mapping[str, object], linha) for linha in resultado.mappings()]
        return analisar_apostas(linhas, fuso_horario=fuso_horario)
