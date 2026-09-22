from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.dialects import postgresql

from bancaemdia import models
from bancaemdia.domain import registros
from bancaemdia.repositories.banca_repo import BancaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.extrato_repo import ExtratoRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo, _fim_exclusivo_no_brasil
from bancaemdia.repositories.movimento_requisicao_repo import MovimentoRequisicaoRepo

AGORA = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
ONTEM = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
TRANSFERENCIA = UUID("12345678-1234-5678-1234-567812345678")


class _Result:
    def __init__(self, valor: Any) -> None:
        self.valor = valor

    def all(self) -> list[Any]:
        return list(self.valor)

    def scalars(self) -> Any:
        return iter(self.valor)

    def scalar_one(self) -> Any:
        return self.valor[0] if isinstance(self.valor, list) else self.valor

    def scalar_one_or_none(self) -> Any:
        if not self.valor:
            return None
        return self.valor[0] if isinstance(self.valor, list) else self.valor


class _Session:
    def __init__(self, *respostas: Any) -> None:
        self.respostas = list(respostas)
        self.statements: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self.statements.append(statement)
        return _Result(self.respostas.pop(0) if self.respostas else [])


class _Linha:
    def __init__(self, movimento: models.Movimento, total: int) -> None:
        self.movimento = movimento
        self.total = total

    def __getitem__(self, indice: int) -> models.Movimento:
        assert indice == 0
        return self.movimento


class _LinhaExtrato:
    def __init__(self, id_: int, total: int) -> None:
        self.origem = "movimento"
        self.id = id_
        self.conta_casa_id = 3
        self.tipo = "TRANSFERENCIA"
        self.data_referencia = AGORA
        self.data_referencia_origem = "ocorrido_em"
        self.valor_centavos = -50_000
        self.transferencia_id = TRANSFERENCIA
        self.ocorrido_em = AGORA
        self.descricao = "entre casas"
        self.chave = None
        self.estado = None
        self.stake_centavos = None
        self.retorno_centavos = None
        self.resultado_liquido_centavos = None
        self.revisao_grave = None
        self.total = total


class _LinhaSaldo:
    conta_casa_id = 3
    depositado_centavos = 100_000
    sacado_centavos = 20_000
    bonus_centavos = 5_000
    movido_centavos = -10_000
    apostado_centavos = 30_000
    retornado_centavos = 50_000
    em_jogo_centavos = 10_000
    apostas_pendentes = 1
    movimentos = 4
    apostado_no_periodo_centavos = 30_000
    retornado_no_periodo_centavos = 50_000
    apostas_antes_do_caixa = 2
    desde = date(2026, 9, 1)
    lucro_centavos = 7_000


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: Any) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def _movimento(id_: int = 4) -> models.Movimento:
    return models.Movimento(
        id=id_,
        usuario_id=1,
        conta_casa_id=3,
        tipo="TRANSFERENCIA",
        valor_centavos=-50_000,
        ocorrido_em=ONTEM,
        descricao=None,
        transferencia_id=TRANSFERENCIA,
    )


def _conta(id_: int) -> models.ContaCasa:
    return models.ContaCasa(
        id=id_, usuario_id=1, casa_id=id_ + 10, apelido="", desde=ONTEM, ate=None, ativa=True
    )


def _banca() -> models.Banca:
    return models.Banca(
        id=8,
        usuario_id=1,
        nome="Principal",
        saldo_inicial_centavos=100_000,
        criado_em=ONTEM,
    )


def _requisicao() -> models.MovimentoRequisicao:
    return models.MovimentoRequisicao(
        id=9,
        usuario_id=1,
        chave_idempotencia="cliente:123",
        requisicao_hash="a" * 64,
        resposta_json={"tipo": "DEPOSITO", "movimentos": []},
        criado_em=AGORA,
    )


def test_transferencia_id_is_optional_uuid_with_a_partial_index() -> None:
    coluna = models.Movimento.__table__.c.transferencia_id
    assert coluna.nullable is True
    assert isinstance(coluna.type, postgresql.UUID)
    indice = next(
        indice
        for indice in models.Movimento.__table__.indexes
        if indice.name == "idx_movimentos_transferencia"
    )
    assert [coluna.name for coluna in indice.columns] == ["usuario_id", "transferencia_id"]
    assert str(indice.dialect_options["postgresql"]["where"]) == "transferencia_id IS NOT NULL"


async def test_movimento_page_applies_filters_and_orders_newest_first() -> None:
    session = _Session([_Linha(_movimento(), 17)])

    movimentos, total = await MovimentoRepo().list_page(
        session,
        1,
        {
            "conta_casa_id": 3,
            "tipo": "TRANSFERENCIA",
            "desde": ONTEM,
            "ate": AGORA,
        },
        pagina=2,
        tamanho=10,
    )

    assert movimentos == [
        registros.Movimento(4, 1, 3, "TRANSFERENCIA", -50_000, ONTEM, None, TRANSFERENCIA)
    ]
    assert total == 17
    sql = _sql(session.statements[0])
    for trecho in (
        "movimentos.usuario_id = ",
        "movimentos.conta_casa_id = ",
        "movimentos.tipo = ",
        "movimentos.ocorrido_em >= ",
        "movimentos.ocorrido_em < ",
        "count(*) OVER ()",
        "ORDER BY movimentos.ocorrido_em DESC, movimentos.id DESC",
        "LIMIT %(param_1)s OFFSET %(param_2)s",
    ):
        assert trecho in sql
    assert _params(session.statements[0])["param_2"] == 10


async def test_movimento_empty_later_page_still_returns_the_filtered_total() -> None:
    session = _Session([], 23)

    movimentos, total = await MovimentoRepo().list_page(
        session, 1, {"tipo": "SAQUE"}, pagina=4, tamanho=10
    )

    assert movimentos == []
    assert total == 23
    assert len(session.statements) == 2
    contagem = _sql(session.statements[1])
    assert contagem.startswith("SELECT count(*) AS count_1")
    assert "movimentos.tipo = " in contagem
    assert "LIMIT" not in contagem and "OFFSET" not in contagem


async def test_saldo_is_aggregated_in_sql_and_returns_at_most_one_row_per_account() -> None:
    session = _Session([_LinhaSaldo()])

    saldos = await MovimentoRepo().aggregate_saldos_by_usuario(
        session,
        1,
        date(2026, 9, 21),
    )

    assert saldos[3].saldo_centavos == 95_000
    assert saldos[3].depositado_centavos == 100_000
    assert saldos[3].apostas_pendentes == 1
    assert saldos[3].apostas_antes_do_caixa == 2
    assert saldos[3].lucro_centavos == 7_000
    sql = _sql(session.statements[0])
    for trecho in (
        "WITH saldo_movimentos AS",
        "saldo_apostas AS",
        "sum(CASE WHEN",
        "count(*) FILTER (WHERE apostas.estado = ",
        "CAST(timezone(",
        "movimentos.usuario_id = ",
        "apostas.usuario_id = ",
        "apostas.selecionada IS true",
        "apostas.revisao_grave IS false",
        "apostas.estado NOT IN",
        "contas_casa.usuario_id = ",
        "GROUP BY movimentos.conta_casa_id",
        "GROUP BY apostas.conta_casa_id, saldo_movimentos.desde",
        "ORDER BY contas_casa.id",
    ):
        assert trecho in sql
    assert "abs(CAST(movimentos.valor_centavos AS NUMERIC))" in sql
    assert "movimentos.ocorrido_em < " in sql
    assert "apostas.data_aposta < " in sql
    assert "apostas.criada_em < " in sql
    assert "CAST(timezone(%(timezone_1)s, movimentos.ocorrido_em) AS DATE) <=" not in sql
    assert (
        "CAST(timezone(%(timezone_2)s, coalesce(apostas.data_aposta"
        not in sql.split("WHERE apostas.usuario_id", maxsplit=1)[1]
    )
    assert "movimentos.id" not in sql
    assert "apostas.id" not in sql

    limites = [
        valor
        for valor in _params(session.statements[0]).values()
        if isinstance(valor, datetime) and valor.tzinfo is not None
    ]
    assert limites
    assert {limite.astimezone(UTC) for limite in limites} == {datetime(2026, 9, 22, 3, tzinfo=UTC)}


def test_corte_civil_brasileiro_vira_limite_utc_exclusivo_sem_estourar_date_max() -> None:
    limite = _fim_exclusivo_no_brasil(date(2026, 9, 21))

    assert limite is not None
    assert limite.astimezone(UTC) == datetime(2026, 9, 22, 3, tzinfo=UTC)
    # Em 2018 o horário de verão pulou a meia-noite brasileira. Mesmo esse limite inexistente
    # escrito no relógio civil representa exatamente o primeiro instante válido do dia seguinte.
    limite_horario_de_verao = _fim_exclusivo_no_brasil(date(2018, 11, 3))
    assert limite_horario_de_verao is not None
    assert limite_horario_de_verao.astimezone(UTC) == datetime(2018, 11, 4, 3, tzinfo=UTC)
    assert _fim_exclusivo_no_brasil(None) is None
    assert _fim_exclusivo_no_brasil(date.max) is None


async def test_idempotency_lookup_is_scoped_by_user_and_key() -> None:
    session = _Session([_requisicao()])

    encontrada = await MovimentoRequisicaoRepo().get(session, 1, "cliente:123")

    assert encontrada == registros.MovimentoRequisicao(
        9,
        1,
        "cliente:123",
        "a" * 64,
        {"tipo": "DEPOSITO", "movimentos": []},
        AGORA,
    )
    sql = _sql(session.statements[0])
    assert "movimento_requisicoes.usuario_id = " in sql
    assert "movimento_requisicoes.chave_idempotencia = " in sql


async def test_extrato_unions_filtered_sources_and_orders_tied_ids_numerically() -> None:
    session = _Session([_LinhaExtrato(10, 23), _LinhaExtrato(9, 23)])

    itens, total = await ExtratoRepo().list_page(
        session,
        1,
        {"conta_casa_id": 3, "desde": ONTEM, "ate": AGORA},
        pagina=2,
        tamanho=2,
    )

    assert [item.id for item in itens] == [10, 9]
    assert itens[0] == registros.LinhaExtrato(
        "movimento",
        10,
        3,
        "TRANSFERENCIA",
        AGORA,
        "ocorrido_em",
        -50_000,
        TRANSFERENCIA,
        AGORA,
        "entre casas",
        None,
        None,
        None,
        None,
        None,
        None,
    )
    assert total == 23
    sql = _sql(session.statements[0])
    for trecho in (
        "UNION ALL",
        "movimentos.usuario_id = ",
        "movimentos.conta_casa_id = ",
        "movimentos.ocorrido_em >= ",
        "movimentos.ocorrido_em < ",
        "apostas.usuario_id = ",
        "apostas.conta_casa_id = ",
        "apostas.selecionada IS true",
        "apostas.estado != ",
        "coalesce(apostas.data_aposta, apostas.criada_em) >= ",
        "coalesce(apostas.data_aposta, apostas.criada_em) < ",
        "count(*) OVER ()",
        "ORDER BY extrato.data_referencia DESC NULLS LAST, extrato.origem, extrato.id DESC",
        "LIMIT",
        "OFFSET",
    ):
        assert trecho in sql
    assert "movimento:" not in sql


async def test_extrato_empty_later_page_counts_the_filtered_union() -> None:
    session = _Session([], 31)

    itens, total = await ExtratoRepo().list_page(
        session, 1, {"conta_casa_id": 3}, pagina=4, tamanho=10
    )

    assert itens == []
    assert total == 31
    assert len(session.statements) == 2
    contagem = _sql(session.statements[1])
    assert contagem.startswith("SELECT count(*) AS count_1")
    assert "UNION ALL" in contagem
    assert "movimentos.conta_casa_id = " in contagem
    assert "apostas.conta_casa_id = " in contagem
    assert "LIMIT" not in contagem and "OFFSET" not in contagem
    assert "ORDER BY" not in contagem


async def test_contas_are_locked_in_deterministic_order_and_scoped_to_user() -> None:
    session = _Session([_conta(3), _conta(7)])

    contas = await ContaCasaRepo().get_many_for_update(session, 1, [7, 3, 7])

    assert [conta.id for conta in contas] == [3, 7]
    sql = _sql(session.statements[0])
    assert "contas_casa.usuario_id = " in sql
    assert "contas_casa.id IN " in sql
    assert "ORDER BY contas_casa.id" in sql
    assert sql.endswith("FOR UPDATE")
    assert _params(session.statements[0])["id_1"] == [3, 7]


async def test_contas_and_bancas_list_only_one_users_rows() -> None:
    contas_session = _Session([_conta(3)])
    bancas_session = _Session([_banca()])

    contas = await ContaCasaRepo().list_by_usuario(contas_session, 1)
    bancas = await BancaRepo().list_by_usuario(bancas_session, 1)

    assert contas == [registros.ContaCasa(3, 1, 13, "", ONTEM, None, True)]
    assert bancas == [registros.Banca(8, 1, "Principal", 100_000, ONTEM)]
    assert "WHERE contas_casa.usuario_id = " in _sql(contas_session.statements[0])
    assert "ORDER BY contas_casa.id" in _sql(contas_session.statements[0])
    assert "WHERE bancas.usuario_id = " in _sql(bancas_session.statements[0])
    assert "ORDER BY bancas.id" in _sql(bancas_session.statements[0])
