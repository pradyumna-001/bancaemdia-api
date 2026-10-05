"""Read the live extension of the dashboard's canonical per-bet view."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.domain.analytics import analisar_apostas
from bancaemdia.domain.painel import FiltrosPainel


class AnalyticsRepo:
    async def consultar(
        self, session: AsyncSession, usuario_id: int, filtros: FiltrosPainel, fuso_horario: str
    ) -> dict[str, object]:
        condicoes = ["usuario_id = :usuario_id", "data < :fim"]
        parametros: dict[str, object] = {
            "usuario_id": usuario_id,
            "fim": filtros.janela.fim,
        }
        if filtros.janela.inicio is not None:
            condicoes.append("data >= :inicio")
            parametros["inicio"] = filtros.janela.inicio
        for coluna in ("casa_id", "tipster_id", "mercado_id"):
            valor = getattr(filtros, coluna)
            if valor is not None:
                condicoes.append(f"{coluna} = :{coluna}")
                parametros[coluna] = valor
        resultado = await session.execute(
            text("SELECT * FROM public.painel_analises_apostas WHERE " + " AND ".join(condicoes)),
            parametros,
        )
        linhas = [cast(Mapping[str, object], linha) for linha in resultado.mappings()]
        return analisar_apostas(linhas, fuso_horario=fuso_horario)
