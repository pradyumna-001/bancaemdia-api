from __future__ import annotations

from typing import Any

from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from bancaemdia.db.seed import seed_canonical
from bancaemdia.domain import vocabulario
from bancaemdia.models.mercado import FAMILIAS

ESPORTES_DA_ISSUE = {
    "Futebol",
    "Basquete",
    "Tênis",
    "MMA",
    "F1",
    "Futebol Americano",
    "Beisebol",
    "Vôlei",
    "Handebol",
    "E-sports",
}
CASAS_DA_ISSUE = {
    "bet365",
    "Betano",
    "Betfair",
    "Sportingbet",
    "Novibet",
    "Vupi",
    "Esportes da Sorte",
}


class _Result:
    def __init__(self, rows: list[tuple[str, int]] | None = None, rowcount: int = 0) -> None:
        self.rows = rows or []
        self.rowcount = rowcount

    def __iter__(self) -> Any:
        return iter(self.rows)

    def tuples(self) -> _Result:
        return self

    def all(self) -> list[tuple[str, int]]:
        return self.rows

    def keys(self) -> list[str]:
        return ["nome", "id"]


class _Conn:
    def __init__(self, esportes: dict[str, int], rowcount: int) -> None:
        self.esportes = esportes
        self.rowcount = rowcount
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if isinstance(statement, Select):
            return _Result(rows=list(self.esportes.items()))
        return _Result(rowcount=self.rowcount)


def _esportes() -> dict[str, int]:
    return {nome: numero for numero, (nome, _) in enumerate(vocabulario.ESPORTES, start=1)}


def _compiled(statement: Any) -> Any:
    return statement.compile(dialect=postgresql.dialect())


def test_houses_are_unique_and_include_the_ones_the_issue_names() -> None:
    nomes = [nome for nome, _ in vocabulario.CASAS]
    dominios = [dominio for _, dominio in vocabulario.CASAS]
    assert len(nomes) == len(set(nomes)) == 54
    assert len(dominios) == len(set(dominios))
    assert all(dominio.endswith(".bet.br") for dominio in dominios)
    assert CASAS_DA_ISSUE <= set(nomes)


def test_sports_are_the_ten_of_the_issue_with_an_icon_each() -> None:
    assert {nome for nome, _ in vocabulario.ESPORTES} == ESPORTES_DA_ISSUE
    assert all(icone for _, icone in vocabulario.ESPORTES)


def test_competitions_point_to_seeded_sports() -> None:
    nomes = [nome for nome, _, _ in vocabulario.COMPETICOES]
    assert len(nomes) == len(set(nomes))
    assert {esporte for _, esporte, _ in vocabulario.COMPETICOES} == ESPORTES_DA_ISSUE
    assert all(pais for _, _, pais in vocabulario.COMPETICOES)


def test_markets_are_the_78_canonical_ones_in_known_families() -> None:
    nomes = [nome for nome, _ in vocabulario.MERCADOS]
    assert len(nomes) == len(set(nomes)) == 78
    assert {familia for _, familia in vocabulario.MERCADOS} <= set(FAMILIAS)
    assert ("Total de Gols", "GOLS") in vocabulario.MERCADOS
    assert ("Ambas as Equipes Marcam", "AMBAS_MARCAM") in vocabulario.MERCADOS


def test_teams_have_unique_names_and_aliases() -> None:
    nomes = [nome for nome, _ in vocabulario.TIMES]
    apelidos = [apelido for _, lista in vocabulario.TIMES for apelido in lista]
    assert len(nomes) == len(set(nomes)) == 31
    assert len(apelidos) == len(set(apelidos))
    assert not set(nomes) & set(apelidos)
    assert all(lista for _, lista in vocabulario.TIMES)


async def test_seed_inserts_every_vocabulary_ignoring_conflicts() -> None:
    conn = _Conn(_esportes(), rowcount=5)
    inserted = await seed_canonical(conn)
    inserts = [s for s in conn.statements if not isinstance(s, Select)]
    assert [str(_compiled(s)).split(" ")[2] for s in inserts] == [
        "casas",
        "esportes",
        "competicoes",
        "mercados",
        "times",
    ]
    assert all("ON CONFLICT DO NOTHING" in str(_compiled(s)) for s in inserts)
    assert inserted == {
        "casas": 5,
        "esportes": 5,
        "competicoes": 5,
        "mercados": 5,
        "times": 5,
    }


async def test_seed_reads_sports_back_before_linking_competitions() -> None:
    conn = _Conn(_esportes(), rowcount=1)
    await seed_canonical(conn)
    assert isinstance(conn.statements[2], Select)
    competicoes = _compiled(conn.statements[3])
    valores = set(competicoes.params.values())
    assert "NBA" in valores
    assert _esportes()["Basquete"] in valores
    assert "Brasileirão Série A" in valores


async def test_seed_rows_carry_the_vocabulary_values() -> None:
    conn = _Conn(_esportes(), rowcount=1)
    await seed_canonical(conn)
    casas, esportes, _, _, mercados, times = conn.statements
    assert {"bet365", "bet365.bet.br"} <= set(_compiled(casas).params.values())
    assert "⚽" in _compiled(esportes).params.values()
    assert "AMBAS_MARCAM" in _compiled(mercados).params.values()
    assert ["Gremio", "Grêmio RS", "Gremio RS", "Grêmio FBPA"] in _compiled(times).params.values()


async def test_second_run_inserts_nothing() -> None:
    conn = _Conn(_esportes(), rowcount=0)
    assert set((await seed_canonical(conn)).values()) == {0}
