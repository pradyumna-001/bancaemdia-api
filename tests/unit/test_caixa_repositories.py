from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.dialects import postgresql

from bancaemdia import models
from bancaemdia.domain import registros
from bancaemdia.repositories.banca_repo import BancaRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.extrato_repo import ExtratoRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo

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
