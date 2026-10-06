from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.sql.elements import TextClause

from bancaemdia.domain.painel import (
    COLUNAS_EXPORTACAO,
    FiltrosPainel,
    GranularidadePainel,
    SecaoExportacao,
)
from bancaemdia.repositories.painel_repo import (
    PainelRepo,
    _sql_evolucao,
    _sql_grupo,
    _sql_por_periodo,
    _sql_resumo,
)


def _metricas(**mudancas: object) -> dict[str, object]:
    valores: dict[str, object] = {
        "total_apostas": 0,
        "pendentes": 0,
        "greens": 0,
        "reds": 0,
        "giro_centavos": Decimal(0),
        "base_roi_centavos": Decimal(0),
        "retorno_centavos": Decimal(0),
        "lucro_centavos": Decimal(0),
        "freebets": 0,
    }
    valores.update(mudancas)
    return valores


class _Mapeamentos:
    def __init__(self, linhas: Iterable[Mapping[str, object]]) -> None:
        self.linhas = tuple(linhas)

    def __iter__(self):
        return iter(self.linhas)

    def __aiter__(self) -> AsyncIterator[Mapping[str, object]]:
        return self._iterar()

    async def _iterar(self) -> AsyncIterator[Mapping[str, object]]:
        for linha in self.linhas:
            yield linha

    def one(self) -> Mapping[str, object]:
        assert len(self.linhas) == 1
        return self.linhas[0]

    def one_or_none(self) -> Mapping[str, object] | None:
        assert len(self.linhas) <= 1
        return self.linhas[0] if self.linhas else None


class _Resultado:
    def __init__(self, linhas: Iterable[Mapping[str, object]]) -> None:
        self._mapeamentos = _Mapeamentos(linhas)
        self.fechado = False

    def mappings(self) -> _Mapeamentos:
        return self._mapeamentos

    async def close(self) -> None:
        self.fechado = True


class _Sessao:
    def __init__(
        self,
        *,
        atualizado_em: datetime | None = None,
        atraso_replica: Decimal | None = None,
    ) -> None:
        self.atualizado_em = atualizado_em
        self.atraso_replica = atraso_replica
        self.execucoes: list[tuple[str, Mapping[str, object]]] = []
        self.streams: list[tuple[str, Mapping[str, object]]] = []
        self.escalares: list[tuple[str, Mapping[str, object] | None]] = []

    def _linhas(self, sql: str) -> list[Mapping[str, object]]:
        if "WITH agregado AS" in sql:
            if "public.painel_por_casa AS d" in sql:
                return [
                    {
                        "id": 5,
                        "nome": "Casa A",
                        "familia": None,
                        **_metricas(
                            total_apostas=2,
                            greens=1,
                            reds=1,
                            lucro_centavos=Decimal("300"),
                            base_roi_centavos=Decimal("1000"),
                        ),
                    }
                ]
            if "public.painel_por_tipster AS d" in sql:
                return [{"id": 7, "nome": "Tipster A", "familia": None, **_metricas()}]
            return [
                {
                    "id": 9,
                    "nome": "Mercado A",
                    "familia": "GOLS",
                    **_metricas(),
                }
            ]
        if "WITH iniciais AS" in sql:
            return [
                {
                    "banca_id": 3,
                    "banca_nome": "Principal",
                    "periodo_inicio": date(2026, 9, 20),
                    "contribuicao_centavos": Decimal("200"),
                    "saldo_inicial_centavos": Decimal("10000"),
                    "acumulado_anterior_centavos": Decimal("500"),
                }
            ]
        if "DATE_TRUNC(" in sql:
            return [
                {
                    "periodo_inicio": date(2026, 9, 15),
                    **_metricas(
                        total_apostas=2,
                        greens=1,
                        reds=1,
                        lucro_centavos=Decimal("300"),
                        base_roi_centavos=Decimal("1000"),
                    ),
                }
            ]
        if "r.saldo_centavos" in sql:
            return [
                {
                    "saldo_centavos": None,
                    "saldo_conhecido_centavos": Decimal("7000"),
                    "contas_saldo_desconhecido": 1,
                }
            ]
        if "FROM public.painel_por_periodo AS p" in sql:
            return [
                _metricas(
                    total_apostas=2,
                    greens=1,
                    reds=1,
                    giro_centavos=Decimal("1000"),
                    base_roi_centavos=Decimal("1000"),
                    retorno_centavos=Decimal("1300"),
                    lucro_centavos=Decimal("300"),
                )
            ]
        raise AssertionError(f"SQL inesperado: {sql}")

    async def execute(
        self,
        statement: TextClause,
        parametros: Mapping[str, object],
    ) -> _Resultado:
        sql = str(statement)
        self.execucoes.append((sql, parametros))
        return _Resultado(self._linhas(sql))

    async def stream(
        self,
        statement: TextClause,
        parametros: Mapping[str, object],
    ) -> _Resultado:
        sql = str(statement)
        self.streams.append((sql, parametros))
        return _Resultado(self._linhas(sql))

    async def scalar(
        self,
        statement: TextClause,
        parametros: Mapping[str, object] | None = None,
    ) -> object:
        sql = str(statement)
        self.escalares.append((sql, parametros))
        if "public.painel_atualizacao" in sql:
            return self.atualizado_em
        return self.atraso_replica


def _filtros() -> FiltrosPainel:
    return FiltrosPainel.criar(
        "7d",
        casa_id=5,
        tipster_id=7,
        mercado_id=9,
        agora=datetime(2026, 9, 21, 12),
    )


def test_filtered_queries_use_the_daily_public_cube_and_every_dimension() -> None:
    filtros = _filtros()
    consultas = [
        _sql_resumo(42, filtros),
        _sql_grupo(SecaoExportacao.POR_CASA, 42, filtros),
        _sql_grupo(SecaoExportacao.POR_TIPSTER, 42, filtros),
        _sql_grupo(SecaoExportacao.POR_MERCADO, 42, filtros),
        _sql_por_periodo(42, filtros),
    ]

    for sql, parametros in consultas:
        assert "FROM public.painel_por_periodo AS p" in sql
        assert "p.usuario_id = :usuario_id" in sql
        assert "p.granularidade = 'dia'" in sql
        assert "p.periodo_inicio >= :inicio" in sql
        assert "p.periodo_inicio < :fim" in sql
        assert "p.casa_id = :casa_id" in sql
        assert "p.tipster_id = :tipster_id" in sql
        assert "p.mercado_id = :mercado_id" in sql
        assert parametros == {
            "usuario_id": 42,
            "inicio": date(2026, 9, 15),
            "fim": date(2026, 9, 22),
            "casa_id": 5,
            "tipster_id": 7,
            "mercado_id": 9,
        }
        assert "painel.mv_" not in sql


def test_evolution_fetches_initial_balance_and_pre_window_history_separately() -> None:
    sql, parametros = _sql_evolucao(42, _filtros())

    assert sql.count("FROM public.painel_evolucao_banca") == 3
    assert "i.usuario_id = :usuario_id" in sql
    assert "h.usuario_id = :usuario_id" in sql
    assert "e.usuario_id = :usuario_id" in sql
    assert "h.data < :inicio" in sql
    assert "e.data >= :inicio" in sql
    assert "MAX(i.saldo_inicial_centavos)" in sql
    assert "acumulado_anterior_centavos" in sql
    assert parametros["usuario_id"] == 42
    assert parametros["inicio"] == date(2026, 9, 15)


@pytest.mark.asyncio
async def test_consultar_keeps_filtered_metrics_and_all_time_cash_distinct() -> None:
    respondido_em = datetime(2026, 9, 21, 15, 0, 10, tzinfo=UTC)
    sessao = _Sessao(
        atualizado_em=datetime(2026, 9, 21, 15, tzinfo=UTC),
        atraso_replica=Decimal("1.25"),
    )

    painel = await PainelRepo().consultar(  # type: ignore[arg-type]
        sessao,
        42,
        _filtros(),
        respondido_em=respondido_em,
    )

    assert painel.resumo.lucro_centavos == 300
    assert painel.resumo.roi == Decimal("0.3")
    assert painel.saldo.saldo_total_centavos is None
    assert painel.saldo.saldo_conhecido_centavos == 7_000
    assert painel.saldo.contas_saldo_desconhecido == 1
    assert painel.saldo.escopo == "contas_casa_all_time"
    assert painel.por_casa[0].nome == "Casa A"
    assert painel.por_mercado[0].familia == "GOLS"
    assert painel.evolucao[0].acumulado_centavos == 700
    assert painel.evolucao[0].saldo_centavos == 10_700
    assert painel.frescor.atualizado_em == datetime(2026, 9, 21, 15, tzinfo=UTC)
    assert painel.frescor.idade_mv_segundos == Decimal(10)
    assert painel.frescor.replica_atraso_segundos == Decimal("1.25")

    saldo_sql, saldo_parametros = next(
        (sql, parametros) for sql, parametros in sessao.execucoes if "r.saldo_centavos" in sql
    )
    assert "public.painel_resumo" in saldo_sql
    assert saldo_parametros == {"usuario_id": 42}
    assert not {"inicio", "fim", "casa_id", "tipster_id", "mercado_id"} & set(saldo_parametros)

    for sql, parametros in (*sessao.execucoes, *sessao.streams):
        assert "public.painel_" in sql
        assert "painel.mv_" not in sql
        assert ":usuario_id" in sql
        assert parametros["usuario_id"] == 42


@pytest.mark.asyncio
async def test_metrics_reaggregate_daily_rows_into_the_requested_bucket() -> None:
    sessao = _Sessao()
    filtros = FiltrosPainel.criar(
        "90d",
        agora=datetime(2026, 9, 21, 12),
    )

    metricas = await PainelRepo().consultar_metricas(  # type: ignore[arg-type]
        sessao, 42, filtros
    )

    assert metricas.granularidade == GranularidadePainel.SEMANA
    assert metricas.labels == ("2026-09-15",)
    assert all(isinstance(valor, int) for serie in metricas.datasets for valor in serie.data)
    sql, _ = sessao.execucoes[0]
    assert "granularidade = 'dia'" in sql
    assert "DATE_TRUNC('week'" in sql


@pytest.mark.asyncio
async def test_export_streams_each_unbounded_section_with_the_published_columns() -> None:
    sessao = _Sessao()
    secoes = (
        SecaoExportacao.POR_CASA,
        SecaoExportacao.POR_PERIODO,
        SecaoExportacao.EVOLUCAO,
    )

    linhas = [
        linha
        async for linha in PainelRepo().iterar_exportacao(  # type: ignore[arg-type]
            sessao,
            42,
            _filtros(),
            secoes,
        )
    ]

    assert [linha.secao for linha in linhas] == list(secoes)
    assert sessao.execucoes == []
    assert len(sessao.streams) == len(secoes)
    for linha in linhas:
        assert tuple(linha.valores) == COLUNAS_EXPORTACAO[linha.secao]
    assert linhas[-1].valores["saldo_centavos"] == 10_700
    assert all(not isinstance(valor, float) for linha in linhas for valor in linha.valores.values())


@pytest.mark.asyncio
async def test_an_explicit_empty_export_does_not_open_any_cursor() -> None:
    sessao = _Sessao()

    linhas = [
        linha
        async for linha in PainelRepo().iterar_exportacao(  # type: ignore[arg-type]
            sessao,
            42,
            _filtros(),
            (),
        )
    ]

    assert linhas == []
    assert sessao.execucoes == []
    assert sessao.streams == []


@pytest.mark.asyncio
async def test_primary_keeps_replica_lag_null_and_freshness_is_not_refresh_time() -> None:
    atualizado = datetime(2026, 9, 21, 15, tzinfo=UTC)
    respondido = datetime(2026, 9, 21, 15, 0, 5, tzinfo=UTC)
    sessao = _Sessao(atualizado_em=atualizado, atraso_replica=None)

    frescor = await PainelRepo().frescor(  # type: ignore[arg-type]
        sessao, 42, respondido_em=respondido
    )

    assert frescor.atualizado_em == atualizado
    assert frescor.respondido_em == respondido
    assert frescor.idade_mv_segundos == Decimal(5)
    assert frescor.replica_atraso_segundos is None
    atualizacao_sql, parametros = sessao.escalares[0]
    assert "public.painel_atualizacao" in atualizacao_sql
    assert "public.painel_resumo" in atualizacao_sql
    assert "r.usuario_id = :usuario_id" in atualizacao_sql
    assert parametros == {"usuario_id": 42}
