"""Consultas tenant-safe do painel materializado.

Os indicadores filtrados saem sempre do cubo diario publico. As visoes por
eixo sao usadas apenas como lookups de nomes; seus totais all-time nunca sao
misturados com uma janela filtrada.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.db.session import REPLICA_LAG_SQL
from bancaemdia.domain.painel import (
    AcumuladorEvolucaoOrdenada,
    ContribuicaoEvolucao,
    FiltrosPainel,
    FrescorPainel,
    GranularidadePainel,
    GrupoPainel,
    LinhaExportacao,
    MetricasGraficos,
    MetricasPainel,
    Painel,
    PainelInvalidoError,
    PontoPeriodo,
    SaldoPainel,
    SecaoExportacao,
    acumular_evolucao,
    calcular_frescor,
    metricas_para_graficos,
)

_CAMPOS_METRICAS = (
    "total_apostas",
    "pendentes",
    "greens",
    "reds",
    "giro_centavos",
    "base_roi_centavos",
    "retorno_centavos",
    "lucro_centavos",
    "freebets",
)

_EIXOS = {
    SecaoExportacao.POR_CASA: (
        "casa_id",
        "public.painel_por_casa",
        "casa_nome",
        None,
    ),
    SecaoExportacao.POR_TIPSTER: (
        "tipster_id",
        "public.painel_por_tipster",
        "tipster_nome",
        None,
    ),
    SecaoExportacao.POR_MERCADO: (
        "mercado_id",
        "public.painel_por_mercado",
        "mercado_nome",
        "familia",
    ),
}

_BUCKET_POSTGRES = {
    GranularidadePainel.DIA: "day",
    GranularidadePainel.SEMANA: "week",
    GranularidadePainel.MES: "month",
}


def _linha_tipificada(linha: object) -> Mapping[str, object]:
    # RowMapping aceita chaves SQL alem de texto; nossas consultas rotulam toda chave consumida.
    return cast(Mapping[str, object], linha)


def _validar_usuario_id(usuario_id: int) -> None:
    if isinstance(usuario_id, bool) or not isinstance(usuario_id, int) or usuario_id <= 0:
        raise PainelInvalidoError("usuario_id precisa ser um id positivo")


def _somas_metricas(alias: str) -> str:
    return ",\n".join(f"COALESCE(SUM({alias}.{campo}), 0) AS {campo}" for campo in _CAMPOS_METRICAS)


def _selecao_metricas(alias: str) -> str:
    return ",\n".join(f"{alias}.{campo}" for campo in _CAMPOS_METRICAS)


def _condicoes_dimensoes(
    alias: str,
    filtros: FiltrosPainel,
    parametros: dict[str, object],
) -> list[str]:
    condicoes: list[str] = []
    for coluna in ("casa_id", "tipster_id", "mercado_id"):
        valor = getattr(filtros, coluna)
        if valor is not None:
            condicoes.append(f"{alias}.{coluna} = :{coluna}")
            parametros[coluna] = valor
    return condicoes


def _filtro_cubo(
    alias: str,
    usuario_id: int,
    filtros: FiltrosPainel,
) -> tuple[str, dict[str, object]]:
    parametros: dict[str, object] = {
        "usuario_id": usuario_id,
        "fim": filtros.janela.fim,
    }
    condicoes = [
        f"{alias}.usuario_id = :usuario_id",
        f"{alias}.granularidade = 'dia'",
        f"{alias}.periodo_inicio < :fim",
    ]
    if filtros.janela.inicio is not None:
        condicoes.append(f"{alias}.periodo_inicio >= :inicio")
        parametros["inicio"] = filtros.janela.inicio
    condicoes.extend(_condicoes_dimensoes(alias, filtros, parametros))
    return "\n  AND ".join(condicoes), parametros


def _filtro_evolucao(
    alias: str,
    usuario_id: int,
    filtros: FiltrosPainel,
    *,
    antes_da_janela: bool,
) -> tuple[str, dict[str, object]]:
    parametros: dict[str, object] = {"usuario_id": usuario_id}
    condicoes = [f"{alias}.usuario_id = :usuario_id"]
    if antes_da_janela:
        inicio = filtros.janela.inicio
        if inicio is None:
            raise PainelInvalidoError("periodo all nao possui historico anterior")
        condicoes.append(f"{alias}.data < :inicio")
        parametros["inicio"] = inicio
    else:
        condicoes.append(f"{alias}.data < :fim")
        parametros["fim"] = filtros.janela.fim
        if filtros.janela.inicio is not None:
            condicoes.append(f"{alias}.data >= :inicio")
            parametros["inicio"] = filtros.janela.inicio
    condicoes.extend(_condicoes_dimensoes(alias, filtros, parametros))
    return "\n      AND ".join(condicoes), parametros


def _sql_resumo(usuario_id: int, filtros: FiltrosPainel) -> tuple[str, dict[str, object]]:
    onde, parametros = _filtro_cubo("p", usuario_id, filtros)
    return (
        f"""
        SELECT
            {_somas_metricas("p")}
        FROM public.painel_por_periodo AS p
        WHERE {onde}
        """,
        parametros,
    )


def _sql_grupo(
    secao: SecaoExportacao,
    usuario_id: int,
    filtros: FiltrosPainel,
) -> tuple[str, dict[str, object]]:
    coluna_id, visao_nome, coluna_nome, coluna_familia = _EIXOS[secao]
    onde, parametros = _filtro_cubo("p", usuario_id, filtros)
    familia = "NULL::text AS familia"
    if coluna_familia is not None:
        familia = f"d.{coluna_familia} AS familia"
    return (
        f"""
        WITH agregado AS (
            SELECT
                p.{coluna_id} AS id,
                {_somas_metricas("p")}
            FROM public.painel_por_periodo AS p
            WHERE {onde}
            GROUP BY p.{coluna_id}
        )
        SELECT
            a.id,
            d.{coluna_nome} AS nome,
            {familia},
            {_selecao_metricas("a")}
        FROM agregado AS a
        LEFT JOIN {visao_nome} AS d
          ON d.usuario_id = :usuario_id
         AND d.{coluna_id} = a.id
        ORDER BY a.lucro_centavos DESC, a.id
        """,
        parametros,
    )


def _sql_por_periodo(
    usuario_id: int,
    filtros: FiltrosPainel,
) -> tuple[str, dict[str, object]]:
    onde, parametros = _filtro_cubo("p", usuario_id, filtros)
    bucket = _BUCKET_POSTGRES[filtros.janela.granularidade]
    return (
        f"""
        SELECT
            DATE_TRUNC('{bucket}', p.periodo_inicio::timestamp)::date AS periodo_inicio,
            {_somas_metricas("p")}
        FROM public.painel_por_periodo AS p
        WHERE {onde}
        GROUP BY 1
        ORDER BY 1
        """,
        parametros,
    )


def _sql_evolucao(
    usuario_id: int,
    filtros: FiltrosPainel,
) -> tuple[str, dict[str, object]]:
    onde_periodo, parametros = _filtro_evolucao("e", usuario_id, filtros, antes_da_janela=False)
    caixa_dimensao = (
        "AND FALSE" if filtros.tipster_id is not None or filtros.mercado_id is not None else ""
    )
    if filtros.casa_id is not None:
        caixa_dimensao += " AND cc.casa_id = :casa_id"
        parametros["casa_id"] = filtros.casa_id
    inicio = filtros.janela.inicio
    if inicio is None:
        historico = """
        historico AS (
            SELECT i.banca_id, 0::numeric AS acumulado_anterior_centavos
            FROM iniciais AS i
        )
        """
    else:
        onde_historico, parametros_historico = _filtro_evolucao(
            "h", usuario_id, filtros, antes_da_janela=True
        )
        parametros.update(parametros_historico)
        historico = f"""
        historico AS (
            SELECT banca_id, COALESCE(SUM(valor), 0)::numeric AS acumulado_anterior_centavos
            FROM (
                SELECT h.banca_id, h.lucro_centavos AS valor
                FROM public.painel_evolucao_banca AS h
                WHERE {onde_historico}
                UNION ALL
                SELECT x.banca_id, x.valor_centavos AS valor
                FROM caixa AS x WHERE x.data < :inicio
            ) AS anteriores
            GROUP BY banca_id
        )
        """
    bucket = _BUCKET_POSTGRES[filtros.janela.granularidade]
    return (
        f"""
        WITH iniciais AS (
            SELECT
                i.banca_id,
                MAX(i.banca_nome) AS banca_nome,
                MAX(i.saldo_inicial_centavos) AS saldo_inicial_centavos
            FROM public.painel_evolucao_banca AS i
            WHERE i.usuario_id = :usuario_id
            GROUP BY i.banca_id
        ),
        caixa AS (
            SELECT
                cc.banca_id,
                (m.ocorrido_em AT TIME ZONE 'America/Sao_Paulo')::date AS data,
                m.valor_centavos::numeric AS valor_centavos
            FROM public.movimentos AS m
            JOIN public.contas_casa AS cc
              ON cc.id = m.conta_casa_id AND cc.usuario_id = m.usuario_id
            JOIN public.bancas AS b
              ON b.id = cc.banca_id AND b.usuario_id = m.usuario_id
            WHERE m.usuario_id = :usuario_id
              AND m.ocorrido_em >= b.criado_em
              {caixa_dimensao}
        ),
        {historico},
        contribuicoes AS (
            SELECT
                e.banca_id,
                DATE_TRUNC('{bucket}', e.data::timestamp)::date AS periodo_inicio,
                COALESCE(SUM(e.lucro_centavos), 0)::numeric AS contribuicao_centavos
            FROM public.painel_evolucao_banca AS e
            WHERE {onde_periodo}
            GROUP BY e.banca_id, 2
            UNION ALL
            SELECT
                x.banca_id,
                DATE_TRUNC('{bucket}', x.data::timestamp)::date AS periodo_inicio,
                COALESCE(SUM(x.valor_centavos), 0)::numeric AS contribuicao_centavos
            FROM caixa AS x
            WHERE x.data < :fim
            {"AND x.data >= :inicio" if inicio is not None else ""}
            GROUP BY x.banca_id, 2
        ),
        agrupadas AS (
            SELECT banca_id, periodo_inicio,
                   SUM(contribuicao_centavos)::numeric AS contribuicao_centavos
            FROM contribuicoes
            GROUP BY banca_id, periodo_inicio
        )
        SELECT
            c.banca_id,
            i.banca_nome,
            c.periodo_inicio,
            c.contribuicao_centavos,
            i.saldo_inicial_centavos,
            COALESCE(h.acumulado_anterior_centavos, 0)::numeric
                AS acumulado_anterior_centavos
        FROM agrupadas AS c
        JOIN iniciais AS i ON i.banca_id = c.banca_id
        LEFT JOIN historico AS h ON h.banca_id = c.banca_id
        ORDER BY c.banca_id, c.periodo_inicio
        """,
        parametros,
    )


def _valores_grupo(grupo: GrupoPainel, *, incluir_familia: bool) -> dict[str, object]:
    valores: dict[str, object] = {
        "id": grupo.id,
        "nome": grupo.nome,
        **grupo.metricas.como_mapeamento(),
    }
    if incluir_familia:
        # Mantem a ordem publicada: id, nome, familia, metricas.
        valores = {
            "id": grupo.id,
            "nome": grupo.nome,
            "familia": grupo.familia,
            **grupo.metricas.como_mapeamento(),
        }
    return valores


def _valores_periodo(ponto: PontoPeriodo) -> dict[str, object]:
    return {
        "periodo_inicio": ponto.periodo_inicio,
        "granularidade": ponto.granularidade.value,
        **ponto.metricas.como_mapeamento(),
    }


class PainelRepo:
    async def _resumo(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
    ) -> MetricasPainel:
        sql, parametros = _sql_resumo(usuario_id, filtros)
        resultado = await session.execute(text(sql), parametros)
        return MetricasPainel.de_linha(_linha_tipificada(resultado.mappings().one()))

    async def _saldo(self, session: AsyncSession, usuario_id: int) -> SaldoPainel:
        resultado = await session.execute(
            text(
                """
                SELECT
                    r.saldo_centavos,
                    r.saldo_conhecido_centavos,
                    r.contas_saldo_desconhecido
                FROM public.painel_resumo AS r
                WHERE r.usuario_id = :usuario_id
                """
            ),
            {"usuario_id": usuario_id},
        )
        linha = resultado.mappings().one_or_none()
        return SaldoPainel.de_linha(None if linha is None else _linha_tipificada(linha))

    async def _grupos(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
        secao: SecaoExportacao,
    ) -> tuple[GrupoPainel, ...]:
        sql, parametros = _sql_grupo(secao, usuario_id, filtros)
        resultado = await session.execute(text(sql), parametros)
        return tuple(
            GrupoPainel.de_linha(_linha_tipificada(linha)) for linha in resultado.mappings()
        )

    async def _pontos_periodo(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
    ) -> tuple[PontoPeriodo, ...]:
        sql, parametros = _sql_por_periodo(usuario_id, filtros)
        resultado = await session.execute(text(sql), parametros)
        return tuple(
            PontoPeriodo.de_linha(_linha_tipificada(linha), filtros.janela.granularidade)
            for linha in resultado.mappings()
        )

    async def _contribuicoes_evolucao(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
    ) -> tuple[ContribuicaoEvolucao, ...]:
        sql, parametros = _sql_evolucao(usuario_id, filtros)
        resultado = await session.execute(text(sql), parametros)
        return tuple(
            ContribuicaoEvolucao.de_linha(_linha_tipificada(linha))
            for linha in resultado.mappings()
        )

    async def frescor(
        self,
        session: AsyncSession,
        usuario_id: int,
        *,
        respondido_em: datetime | None = None,
    ) -> FrescorPainel:
        _validar_usuario_id(usuario_id)
        # painel_atualizacao nao possui usuario_id. O EXISTS conserva a defesa explicita do repo,
        # alem do filtro tenant-safe que cada um dos dois wrappers ja aplica pelo GUC da sessao.
        atualizado_em = await session.scalar(
            text(
                """
                SELECT a.atualizado_em
                FROM public.painel_atualizacao AS a
                WHERE EXISTS (
                    SELECT 1
                    FROM public.painel_resumo AS r
                    WHERE r.usuario_id = :usuario_id
                )
                """
            ),
            {"usuario_id": usuario_id},
        )
        if atualizado_em is not None and not isinstance(atualizado_em, datetime):
            raise PainelInvalidoError("atualizado_em precisa ser um instante")
        replica_atraso = await session.scalar(REPLICA_LAG_SQL)
        return calcular_frescor(
            atualizado_em,
            replica_atraso,
            respondido_em=respondido_em,
        )

    async def consultar(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
        *,
        respondido_em: datetime | None = None,
    ) -> Painel:
        _validar_usuario_id(usuario_id)
        resumo = await self._resumo(session, usuario_id, filtros)
        # O caixa e all-time de todas as contas_casa. Nenhum filtro de aposta o altera.
        saldo = await self._saldo(session, usuario_id)
        por_casa = await self._grupos(session, usuario_id, filtros, SecaoExportacao.POR_CASA)
        por_tipster = await self._grupos(session, usuario_id, filtros, SecaoExportacao.POR_TIPSTER)
        por_mercado = await self._grupos(session, usuario_id, filtros, SecaoExportacao.POR_MERCADO)
        contribuicoes = await self._contribuicoes_evolucao(session, usuario_id, filtros)
        frescor = await self.frescor(
            session,
            usuario_id,
            respondido_em=respondido_em,
        )
        return Painel(
            resumo=resumo,
            saldo=saldo,
            por_casa=por_casa,
            por_tipster=por_tipster,
            por_mercado=por_mercado,
            evolucao=acumular_evolucao(contribuicoes),
            frescor=frescor,
        )

    async def consultar_metricas(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
    ) -> MetricasGraficos:
        _validar_usuario_id(usuario_id)
        pontos = await self._pontos_periodo(session, usuario_id, filtros)
        return metricas_para_graficos(
            pontos,
            granularidade_esperada=filtros.janela.granularidade,
        )

    async def _iterar_grupos(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
        secao: SecaoExportacao,
    ) -> AsyncIterator[LinhaExportacao]:
        sql, parametros = _sql_grupo(secao, usuario_id, filtros)
        resultado = await session.stream(text(sql), parametros)
        try:
            async for linha in resultado.mappings():
                yield LinhaExportacao(
                    secao,
                    _valores_grupo(
                        GrupoPainel.de_linha(_linha_tipificada(linha)),
                        incluir_familia=secao == SecaoExportacao.POR_MERCADO,
                    ),
                )
        finally:
            await resultado.close()

    async def _iterar_periodos(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
    ) -> AsyncIterator[LinhaExportacao]:
        sql, parametros = _sql_por_periodo(usuario_id, filtros)
        resultado = await session.stream(text(sql), parametros)
        try:
            async for linha in resultado.mappings():
                ponto = PontoPeriodo.de_linha(
                    _linha_tipificada(linha), filtros.janela.granularidade
                )
                yield LinhaExportacao(SecaoExportacao.POR_PERIODO, _valores_periodo(ponto))
        finally:
            await resultado.close()

    async def _iterar_evolucao(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
    ) -> AsyncIterator[LinhaExportacao]:
        sql, parametros = _sql_evolucao(usuario_id, filtros)
        resultado = await session.stream(text(sql), parametros)
        acumulador = AcumuladorEvolucaoOrdenada()
        try:
            async for linha in resultado.mappings():
                ponto = acumulador.adicionar(
                    ContribuicaoEvolucao.de_linha(_linha_tipificada(linha))
                )
                yield LinhaExportacao(
                    SecaoExportacao.EVOLUCAO,
                    {
                        "periodo_inicio": ponto.periodo_inicio,
                        "banca_id": ponto.banca_id,
                        "banca_nome": ponto.banca_nome,
                        "contribuicao_centavos": ponto.contribuicao_centavos,
                        "acumulado_centavos": ponto.acumulado_centavos,
                        "saldo_centavos": ponto.saldo_centavos,
                    },
                )
        finally:
            await resultado.close()

    async def iterar_exportacao(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: FiltrosPainel,
        secoes: Iterable[SecaoExportacao] | None = None,
    ) -> AsyncIterator[LinhaExportacao]:
        """Itera cada cursor sem materializar o conjunto exportado na memoria."""

        _validar_usuario_id(usuario_id)
        fonte_secoes = tuple(SecaoExportacao) if secoes is None else secoes
        selecionadas = tuple(dict.fromkeys(SecaoExportacao(secao) for secao in fonte_secoes))
        for secao in selecionadas:
            if secao == SecaoExportacao.RESUMO:
                resumo = await self._resumo(session, usuario_id, filtros)
                saldo = await self._saldo(session, usuario_id)
                yield LinhaExportacao(
                    secao,
                    {
                        **resumo.como_mapeamento(),
                        "saldo_total_centavos": saldo.saldo_total_centavos,
                        "saldo_conhecido_centavos": saldo.saldo_conhecido_centavos,
                        "contas_saldo_desconhecido": saldo.contas_saldo_desconhecido,
                        "saldo_escopo": saldo.escopo.value,
                    },
                )
            elif secao in _EIXOS:
                async for linha in self._iterar_grupos(session, usuario_id, filtros, secao):
                    yield linha
            elif secao == SecaoExportacao.POR_PERIODO:
                async for linha in self._iterar_periodos(session, usuario_id, filtros):
                    yield linha
            elif secao == SecaoExportacao.EVOLUCAO:
                async for linha in self._iterar_evolucao(session, usuario_id, filtros):
                    yield linha
