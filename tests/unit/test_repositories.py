from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from bancaemdia import models
from bancaemdia.domain import registros
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.base import colunas
from bancaemdia.repositories.casa_repo import CasaRepo
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
from bancaemdia.repositories.coleta_token_repo import ColetaTokenRepo
from bancaemdia.repositories.conta_casa_repo import ContaCasaRepo
from bancaemdia.repositories.evento_repo import EventoRepo
from bancaemdia.repositories.movimento_repo import MovimentoRepo
from bancaemdia.repositories.revisao_pendente_repo import RevisaoPendenteRepo
from bancaemdia.repositories.unidade_repo import UnidadeRepo
from bancaemdia.repositories.usuario_repo import UsuarioRepo

AGORA = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
ONTEM = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _Result:
    def __init__(self, objs: list[Any]) -> None:
        self.objs = objs

    def scalar_one_or_none(self) -> Any:
        return self.objs[0] if self.objs else None

    def scalar_one(self) -> Any:
        return self.objs[0]

    def scalars(self) -> Any:
        return iter(self.objs)

    def all(self) -> list[Any]:
        return self.objs


class _Session:
    def __init__(self, *objs: Any) -> None:
        self.objs = list(objs)
        self.statements: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self.statements.append(statement)
        return _Result(self.objs)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: Any) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def _usuario() -> models.Usuario:
    return models.Usuario(id=1, email="Ana@Exemplo.com", nome="Ana", criado_em=AGORA, ativo=True)


def _aposta(**mudancas: Any) -> models.Aposta:
    valores: dict[str, Any] = {
        "id": 10,
        "usuario_id": 1,
        "chave": "t:1:1:0",
        "chat_id": 1,
        "message_id": 1,
        "ordem_na_mensagem": 0,
        "midia_hash": None,
        "banca_id": None,
        "conta_casa_id": 3,
        "tipster_id": None,
        "time_casa_id": None,
        "time_fora_id": None,
        "mercado_id": None,
        "competicao_id": None,
        "data_aposta": ONTEM,
        "data_jogo": None,
        "stake_unidades": 1.0,
        "stake_centavos": 10_000,
        "valor_aposta_centavos": 10_000,
        "odd": 1.9,
        "retorno_centavos": None,
        "estado": "PENDENTE",
        "origem": "telegram",
        "freebet": False,
        "duvida_de_par": False,
        "parceira_chave": None,
        "duplicada_de": None,
        "revisao_grave": False,
        "criada_em": AGORA,
        "atualizada_em": AGORA,
    }
    valores.update(mudancas)
    return models.Aposta(**valores)


def test_colunas_reads_every_mapped_column() -> None:
    dados = colunas(_usuario())
    assert dados == {
        "id": 1,
        "email": "Ana@Exemplo.com",
        "nome": "Ana",
        "criado_em": AGORA,
        "ativo": True,
    }
    assert set(colunas(_aposta())) == {c.name for c in models.Aposta.__table__.columns}


def test_records_are_frozen() -> None:
    usuario = registros.Usuario(**colunas(_usuario()))
    with pytest.raises(FrozenInstanceError):
        usuario.nome = "Outra"


async def test_usuario_get_by_id() -> None:
    session = _Session(_usuario())
    usuario = await UsuarioRepo().get_by_id(session, 1)
    assert usuario == registros.Usuario(1, "Ana@Exemplo.com", "Ana", AGORA, True)
    assert "WHERE usuarios.id = %(id_1)s" in _sql(session.statements[0])
    assert _params(session.statements[0])["id_1"] == 1


async def test_usuario_get_by_email_ignores_case() -> None:
    session = _Session(_usuario())
    await UsuarioRepo().get_by_email(session, "ANA@exemplo.com")
    assert "WHERE lower(usuarios.email) = %(lower_1)s" in _sql(session.statements[0])
    assert _params(session.statements[0])["lower_1"] == "ana@exemplo.com"


async def test_usuario_missing_is_none() -> None:
    session = _Session()
    assert await UsuarioRepo().get_by_id(session, 99) is None
    assert await UsuarioRepo().get_by_email(session, "x@y") is None


async def test_usuario_create_returns_the_inserted_row() -> None:
    session = _Session(_usuario())
    usuario = await UsuarioRepo().create(session, "Ana@Exemplo.com", "Ana")
    assert usuario.id == 1
    sql = _sql(session.statements[0])
    assert sql.startswith("INSERT INTO usuarios (email, nome) VALUES")
    assert "RETURNING usuarios.id, usuarios.email, usuarios.nome" in sql


async def test_aposta_get_by_chave_filters_by_user_and_key() -> None:
    session = _Session(_aposta())
    aposta = await ApostaRepo().get_by_chave(session, 1, "t:1:1:0")
    assert isinstance(aposta, registros.Aposta)
    assert aposta.chave == "t:1:1:0"
    assert aposta.stake_centavos == 10_000
    sql = _sql(session.statements[0])
    assert "WHERE apostas.usuario_id = %(usuario_id_1)s AND apostas.chave = %(chave_1)s" in sql


async def test_aposta_list_without_filters_orders_newest_first() -> None:
    session = _Session(_aposta(id=2), _aposta(id=1))
    apostas = await ApostaRepo().list_by_usuario(session, 1)
    assert [a.id for a in apostas] == [2, 1]
    sql = _sql(session.statements[0])
    assert sql.count("WHERE") == 1
    assert "ORDER BY apostas.criada_em DESC, apostas.id DESC" in sql
    assert "LIMIT" not in sql


async def test_aposta_list_applies_every_filter() -> None:
    session = _Session()
    await ApostaRepo().list_by_usuario(
        session,
        1,
        estado="GREEN",
        origem="telegram",
        banca_id=5,
        conta_casa_id=3,
        desde=ONTEM,
        ate=AGORA,
        limite=20,
    )
    sql = _sql(session.statements[0])
    for trecho in (
        "apostas.estado = %(estado_1)s",
        "apostas.origem = %(origem_1)s",
        "apostas.banca_id = %(banca_id_1)s",
        "apostas.conta_casa_id = %(conta_casa_id_1)s",
        "apostas.data_aposta >= %(data_aposta_1)s",
        "apostas.data_aposta < %(data_aposta_2)s",
        "LIMIT %(param_1)s",
    ):
        assert trecho in sql
    assert _params(session.statements[0])["param_1"] == 20


async def test_aposta_get_by_chave_for_update_locks_the_row() -> None:
    session = _Session(_aposta())

    aposta = await ApostaRepo().get_by_chave_for_update(session, 1, "t:1:1:0")

    assert aposta is not None and aposta.chave == "t:1:1:0"
    assert "FOR UPDATE" in _sql(session.statements[0])


async def test_aposta_page_counts_the_filter_in_the_same_query() -> None:
    class _Linha:
        def __init__(self, aposta: Any, total: int) -> None:
            self.aposta = aposta
            self.total = total

        def __getitem__(self, indice: int) -> Any:
            return self.aposta

    class _Session2(_Session):
        async def execute(self, statement: Any, params: Any = None) -> Any:
            self.statements.append(statement)

            class _R:
                def all(self) -> list[Any]:
                    return [_Linha(_aposta(), 137)]

            return _R()

    session = _Session2()

    apostas, total = await ApostaRepo().list_page(session, 1, {}, 3, 25)

    assert (len(apostas), total) == (1, 137)
    sql = _sql(session.statements[0])
    # O total vem na mesma ida ao banco, e a apagada fica de fora até alguém pedir.
    assert "count(*) OVER ()" in sql
    assert "AND apostas.selecionada" in sql
    assert "LIMIT %(param_1)s OFFSET %(param_2)s" in sql
    assert "ORDER BY apostas.criada_em DESC, apostas.id DESC" in sql


async def test_aposta_page_applies_every_filter_of_the_list() -> None:
    session = _Session()

    await ApostaRepo().list_page(
        session,
        1,
        {
            "estado": "GREEN",
            "origem": "telegram",
            "tipster_id": 2,
            "mercado_id": 3,
            "competicao_id": 4,
            "revisao_grave": True,
            "casa_id": 5,
            "desde": AGORA,
            "ate": AGORA,
            "incluir_apagadas": True,
        },
        1,
        10,
    )

    sql = _sql(session.statements[0])
    for coluna in ("estado", "origem", "tipster_id", "mercado_id", "competicao_id"):
        assert f"apostas.{coluna} = " in sql
    assert "apostas.revisao_grave = " in sql
    # A aposta guarda a conta da casa, não a casa: o filtro por casa passa pelas contas.
    assert "apostas.conta_casa_id IN (SELECT contas_casa.id" in sql
    assert "apostas.data_aposta >= " in sql and "apostas.data_aposta < " in sql
    # Com `incluir_apagadas`, a coluna sai do filtro (ela continua na lista de colunas lidas).
    assert "WHERE" in sql and "AND apostas.selecionada" not in sql


async def test_revisao_lists_the_open_ones_of_one_bet() -> None:
    session = _Session()

    await RevisaoPendenteRepo().list_abertas_by_aposta_chave(session, 1, "t:1:1:0")

    sql = _sql(session.statements[0])
    assert "revisao_pendente.resolvido_em IS NULL" in sql
    assert "extracao_bruta ->> %(extracao_bruta_1)s" in sql


async def test_conta_casa_get_by_id_is_scoped_to_the_person() -> None:
    session = _Session()

    await ContaCasaRepo().get_by_id(session, 1, 42)

    sql = _sql(session.statements[0])
    assert "contas_casa.usuario_id = " in sql and "contas_casa.id = " in sql


async def test_aposta_upsert_updates_only_mutable_columns_on_the_key() -> None:
    session = _Session(_aposta(odd=2.1))
    dados = {"usuario_id": 1, "chave": "t:1:1:0", "odd": 2.1, "stake_centavos": 10_000}
    aposta = await ApostaRepo().upsert_idempotent(session, dados)
    assert aposta.odd == pytest.approx(2.1)
    sql = _sql(session.statements[0])
    assert "ON CONFLICT (usuario_id, chave) WHERE chave IS NOT NULL DO UPDATE SET" in sql
    assert "odd = excluded.odd" in sql
    assert "stake_centavos = excluded.stake_centavos" in sql
    assert "atualizada_em = now()" in sql
    assert "usuario_id = excluded" not in sql
    assert "chave = excluded" not in sql
    assert "RETURNING apostas.id" in sql


async def test_aposta_update_estado() -> None:
    session = _Session(_aposta(estado="GREEN", retorno_centavos=19_000))
    aposta = await ApostaRepo().update_estado(session, 1, "t:1:1:0", "GREEN", 19_000)
    assert aposta is not None
    assert aposta.estado == "GREEN"
    sql = _sql(session.statements[0])
    assert sql.startswith("UPDATE apostas SET ")
    assert "estado=%(estado)s" in sql
    assert "retorno_centavos=%(retorno_centavos)s" in sql
    assert "atualizada_em=now()" in sql
    assert "WHERE apostas.usuario_id = %(usuario_id_1)s AND apostas.chave = %(chave_1)s" in sql
    assert await ApostaRepo().update_estado(_Session(), 1, "nada", "RED") is None


def _evento() -> models.Evento:
    return models.Evento(
        id=7,
        usuario_id=1,
        tipo="APOSTA_CRIADA",
        payload_json={"odd": 1.9},
        fonte="ia",
        confianca=0.9,
        chat_id=1,
        message_id=1,
        criado_em=AGORA,
        aposta_chave="t:1:1:0",
    )


async def test_evento_append_and_lists() -> None:
    session = _Session(_evento())
    evento = await EventoRepo().append(session, {"usuario_id": 1, "tipo": "APOSTA_CRIADA"})
    assert evento.payload_json == {"odd": 1.9}
    assert "INSERT INTO eventos" in _sql(session.statements[0])
    assert "RETURNING eventos.id" in _sql(session.statements[0])

    await EventoRepo().list_by_aposta_chave(session, 1, "t:1:1:0")
    sql = _sql(session.statements[1])
    assert (
        "eventos.usuario_id = %(usuario_id_1)s AND eventos.aposta_chave = %(aposta_chave_1)s" in sql
    )
    assert sql.endswith("ORDER BY eventos.id")

    await EventoRepo().list_by_usuario(session, 1, limite=50)
    sql = _sql(session.statements[2])
    assert "ORDER BY eventos.criado_em DESC, eventos.id DESC" in sql
    assert "LIMIT %(param_1)s" in sql


def _movimento() -> models.Movimento:
    return models.Movimento(
        id=4,
        usuario_id=1,
        conta_casa_id=3,
        tipo="DEPOSITO",
        valor_centavos=50_000,
        ocorrido_em=ONTEM,
        descricao=None,
    )


async def test_movimento_append_and_lists() -> None:
    session = _Session(_movimento())
    movimento = await MovimentoRepo().append(session, {"usuario_id": 1, "tipo": "DEPOSITO"})
    assert movimento == registros.Movimento(4, 1, 3, "DEPOSITO", 50_000, ONTEM, None)

    await MovimentoRepo().list_by_usuario(session, 1, desde=ONTEM, ate=AGORA)
    sql = _sql(session.statements[1])
    assert "movimentos.ocorrido_em >= %(ocorrido_em_1)s" in sql
    assert "movimentos.ocorrido_em < %(ocorrido_em_2)s" in sql
    assert sql.endswith("ORDER BY movimentos.ocorrido_em, movimentos.id")

    await MovimentoRepo().list_by_conta_casa(session, 3)
    assert "WHERE movimentos.conta_casa_id = %(conta_casa_id_1)s" in _sql(session.statements[2])


def _conta() -> models.ContaCasa:
    return models.ContaCasa(
        id=3, usuario_id=1, casa_id=2, apelido="", desde=ONTEM, ate=None, ativa=True
    )


async def test_conta_casa_lookup_create_and_close() -> None:
    session = _Session(_conta())
    conta = await ContaCasaRepo().get_by_usuario_casa(session, 1, 2)
    assert conta == registros.ContaCasa(3, 1, 2, "", ONTEM, None, True, estado=None)
    sql = _sql(session.statements[0])
    assert (
        "contas_casa.usuario_id = %(usuario_id_1)s AND contas_casa.casa_id = %(casa_id_1)s" in sql
    )
    assert "contas_casa.ativa IS true" in sql
    assert "ORDER BY contas_casa.id" in sql
    assert "LIMIT %(param_1)s" in sql

    await ContaCasaRepo().create(session, {"usuario_id": 1, "casa_id": 2})
    assert "INSERT INTO contas_casa" in _sql(session.statements[1])

    await ContaCasaRepo().update_ate(session, 1, 3, AGORA)
    sql = _sql(session.statements[2])
    assert sql.startswith("UPDATE contas_casa SET ate=%(ate)s")
    assert "WHERE contas_casa.usuario_id = %(usuario_id_1)s AND contas_casa.id = %(id_1)s" in sql
    assert await ContaCasaRepo().update_ate(_Session(), 1, 99, None) is None


async def test_unidade_vigente_uses_the_temporal_rule() -> None:
    unidade = models.Unidade(
        id=5, usuario_id=1, valor_centavos=5_000, vigente_de=ONTEM, vigente_ate=None
    )
    session = _Session(unidade)
    vigente = await UnidadeRepo().get_vigente(session, 1, AGORA)
    assert vigente == registros.Unidade(5, 1, 5_000, ONTEM, None)
    sql = _sql(session.statements[0])
    assert "unidades.vigente_de <= %(vigente_de_1)s" in sql
    assert "(unidades.vigente_ate IS NULL OR unidades.vigente_ate > %(vigente_ate_1)s)" in sql
    assert "ORDER BY unidades.vigente_de DESC, unidades.id DESC" in sql
    assert "LIMIT %(param_1)s" in sql
    assert _params(session.statements[0])["vigente_de_1"] == AGORA

    await UnidadeRepo().create(session, {"usuario_id": 1, "valor_centavos": 5_000})
    assert "INSERT INTO unidades" in _sql(session.statements[1])
    assert await UnidadeRepo().get_vigente(_Session(), 1, AGORA) is None


def _coleta() -> models.ColetaCasa:
    return models.ColetaCasa(
        id=8,
        usuario_id=1,
        casa_id=2,
        identidade="B123",
        hash_conteudo="abc",
        bruto_json={"estado": "open"},
        recebido_em=AGORA,
        processado_em=None,
    )


async def test_coleta_casa_lookup_and_idempotent_upsert() -> None:
    session = _Session(_coleta())
    coleta = await ColetaCasaRepo().get_by_identidade(session, 1, 2, "B123")
    assert coleta is not None
    assert coleta.bruto_json == {"estado": "open"}
    assert "coletas_casa.identidade = %(identidade_1)s" in _sql(session.statements[0])

    dados = {"usuario_id": 1, "casa_id": 2, "identidade": "B123", "hash_conteudo": "abc"}
    await ColetaCasaRepo().upsert_idempotent(session, dados)
    sql = _sql(session.statements[1])
    assert "ON CONFLICT ON CONSTRAINT uq_coletas_casa_identidade DO UPDATE SET" in sql
    assert "hash_conteudo = excluded.hash_conteudo" in sql
    assert "bruto_json = excluded.bruto_json" in sql
    assert "processado_em = %(param_1)s" in sql
    assert "WHERE coletas_casa.hash_conteudo IS DISTINCT FROM excluded.hash_conteudo" in sql
    assert "RETURNING coletas_casa.id" in sql


async def test_coleta_casa_upsert_returns_none_when_nothing_changed() -> None:
    dados = {"usuario_id": 1, "casa_id": 2, "identidade": "B123", "hash_conteudo": "abc"}
    assert await ColetaCasaRepo().upsert_idempotent(_Session(), dados) is None


async def test_coleta_casa_is_locked_by_id_counted_by_day_and_marked_processed() -> None:
    session = _Session(_coleta())
    coleta = await ColetaCasaRepo().get_by_id_for_update(session, 1, 8)
    assert coleta is not None and coleta.identidade == "B123"
    assert "coletas_casa.id = %(id_1)s" in _sql(session.statements[0])
    assert _sql(session.statements[0]).endswith("FOR UPDATE")
    assert await ColetaCasaRepo().get_by_id_for_update(_Session(), 1, 9) is None

    contagem = _Session(3)
    assert await ColetaCasaRepo().count_received_since(contagem, 1, AGORA) == 3
    assert "count(*)" in _sql(contagem.statements[0])
    assert "coletas_casa.recebido_em >= %(recebido_em_1)s" in _sql(contagem.statements[0])

    marcada = _Session()
    await ColetaCasaRepo().set_processado(marcada, 1, 8)
    sql = _sql(marcada.statements[0])
    assert "UPDATE coletas_casa SET processado_em=now()" in sql
    assert "coletas_casa.usuario_id = %(usuario_id_1)s" in sql


async def test_coleta_casa_is_locked_by_its_house_identity() -> None:
    session = _Session(_coleta())

    coleta = await ColetaCasaRepo().get_by_identidade_for_update(session, 1, 2, "B123")

    assert coleta is not None and coleta.id == 8
    sql = _sql(session.statements[0])
    assert "coletas_casa.identidade = %(identidade_1)s" in sql
    assert sql.endswith("FOR UPDATE")
    assert await ColetaCasaRepo().get_by_identidade_for_update(_Session(), 1, 2, "X") is None


async def test_coleta_casa_is_read_by_id_and_listed_by_house_after_a_cursor() -> None:
    session = _Session(_coleta())
    coleta = await ColetaCasaRepo().get_by_id(session, 1, 8)
    assert coleta is not None and coleta.identidade == "B123"
    assert "FOR UPDATE" not in _sql(session.statements[0])
    assert await ColetaCasaRepo().get_by_id(_Session(), 1, 9) is None

    lista = _Session(_coleta())
    (linha,) = await ColetaCasaRepo().list_by_casa(lista, 1, 2, 4, 1000)
    assert linha.bruto_json == {"estado": "open"}
    sql = _sql(lista.statements[0])
    assert "coletas_casa.casa_id = %(casa_id_1)s" in sql
    assert "coletas_casa.id > %(id_1)s" in sql
    assert "ORDER BY coletas_casa.id" in sql
    assert _params(lista.statements[0])["id_1"] == 4


async def test_bets_are_counted_by_origin_with_their_messages_and_reviews() -> None:
    session = _Session(("telegram", 5, 2, 4, 1))

    contagens = await ApostaRepo().count_by_origem(session, 1, ONTEM, AGORA)

    assert contagens == [registros.ApostasPorOrigem("telegram", 5, 2, 4, 1)]
    sql = _sql(session.statements[0])
    assert "count(DISTINCT (apostas.chat_id, apostas.message_id))" in sql
    assert "FILTER (WHERE apostas.revisao_grave IS true)" in sql
    assert "apostas.data_aposta >= %(data_aposta_1)s" in sql
    assert "apostas.data_aposta < %(data_aposta_2)s" in sql
    assert "GROUP BY apostas.origem" in sql


async def test_active_users_are_listed_by_id_after_a_cursor() -> None:
    session = _Session(4, 9)

    assert await UsuarioRepo().list_active_ids(session, 3, 2) == [4, 9]

    sql = _sql(session.statements[0])
    assert "usuarios.id > %(id_1)s" in sql
    assert "usuarios.ativo IS true" in sql
    assert "ORDER BY usuarios.id" in sql
    assert "LIMIT" in sql


async def test_coleta_token_lookup_offers_its_hash_to_the_row_policy() -> None:
    session = _Session(7)

    assert await ColetaTokenRepo().get_usuario_id_by_hash(session, "abc") == 7
    assert "set_config('app.coleta_token_hash', :hash, true)" in str(session.statements[0])
    sql = _sql(session.statements[1])
    assert "coleta_token.token_hash = %(token_hash_1)s" in sql
    assert "coleta_token.ativo IS true" in sql
    assert "coleta_token.expira_em IS NULL OR coleta_token.expira_em > now()" in sql
    assert await ColetaTokenRepo().get_usuario_id_by_hash(_Session(), "abc") is None


async def test_coleta_token_is_created_and_deactivated_per_user() -> None:
    criado = _Session(5)
    assert await ColetaTokenRepo().create(criado, 1, "abc", AGORA) == 5
    assert "INSERT INTO coleta_token" in _sql(criado.statements[0])
    assert _params(criado.statements[0])["token_hash"] == "abc"

    desativados = _Session(1, 2)
    assert await ColetaTokenRepo().deactivate_by_usuario(desativados, 1) == 2
    sql = _sql(desativados.statements[0])
    assert "UPDATE coleta_token SET ativo=%(ativo)s" in sql
    assert "coleta_token.ativo IS true" in sql


async def test_casa_is_found_by_its_official_name_and_by_id() -> None:
    por_nome = _Session(2)
    assert await CasaRepo().get_id_by_nome(por_nome, "Betano") == 2
    assert "casas.nome = %(nome_1)s" in _sql(por_nome.statements[0])

    por_id = _Session("Betano")
    assert await CasaRepo().get_nome_by_id(por_id, 2) == "Betano"
    assert "casas.id = %(id_1)s" in _sql(por_id.statements[0])


def _revisao() -> models.RevisaoPendente:
    return models.RevisaoPendente(
        id=6,
        usuario_id=1,
        midia_hash="h",
        motivo="odd fora da faixa",
        extracao_bruta=None,
        criado_em=AGORA,
        resolvido_em=None,
    )


async def test_revisao_pendente_create_list_and_resolve() -> None:
    session = _Session(_revisao())
    revisao = await RevisaoPendenteRepo().create(session, {"usuario_id": 1, "motivo": "odd"})
    assert revisao == registros.RevisaoPendente(6, 1, "h", "odd fora da faixa", None, AGORA, None)

    await RevisaoPendenteRepo().list_by_usuario(session, 1)
    sql = _sql(session.statements[1])
    assert "revisao_pendente.resolvido_em IS NULL" in sql
    assert sql.endswith("ORDER BY revisao_pendente.criado_em, revisao_pendente.id")

    await RevisaoPendenteRepo().list_by_usuario(session, 1, apenas_abertas=False)
    assert "resolvido_em IS NULL" not in _sql(session.statements[2])

    await RevisaoPendenteRepo().resolve(session, 1, 6)
    sql = _sql(session.statements[3])
    assert sql.startswith("UPDATE revisao_pendente SET resolvido_em=now()")
    assert "revisao_pendente.id = %(id_1)s AND revisao_pendente.resolvido_em IS NULL" in sql
    assert await RevisaoPendenteRepo().resolve(_Session(), 1, 6) is None


async def test_revisao_pendente_get_by_id_and_lock_are_tenant_scoped() -> None:
    leitura = _Session(_revisao())
    travada = _Session(_revisao())

    assert await RevisaoPendenteRepo().get_by_id(leitura, 1, 6) == registros.RevisaoPendente(
        6, 1, "h", "odd fora da faixa", None, AGORA, None
    )
    assert await RevisaoPendenteRepo().get_by_id_for_update(
        travada, 1, 6
    ) == registros.RevisaoPendente(6, 1, "h", "odd fora da faixa", None, AGORA, None)

    for session in (leitura, travada):
        sql = _sql(session.statements[0])
        assert "revisao_pendente.usuario_id = " in sql
        assert "revisao_pendente.id = " in sql
        # O detalhe também pode mostrar uma revisão já resolvida; só a resolução decide o 409.
        assert "resolvido_em IS NULL" not in sql
    assert _sql(travada.statements[0]).endswith("FOR UPDATE")
    assert await RevisaoPendenteRepo().get_by_id(_Session(), 1, 99) is None


async def test_revisao_pendente_page_is_open_filtered_fifo_and_has_total() -> None:
    class _Linha:
        def __init__(self, revisao: Any, total: int) -> None:
            self.revisao = revisao
            self.total = total

        def __getitem__(self, indice: int) -> Any:
            return self.revisao

    class _PageSession(_Session):
        async def execute(self, statement: Any, params: Any = None) -> Any:
            self.statements.append(statement)

            class _PageResult:
                def all(self) -> list[Any]:
                    return [_Linha(_revisao(), 17)]

            return _PageResult()

    session = _PageSession()
    revisoes, total = await RevisaoPendenteRepo().list_page(
        session,
        1,
        {"motivo": "odd fora da faixa", "desde": ONTEM, "ate": AGORA},
        pagina=2,
        tamanho=10,
    )

    assert revisoes == [
        registros.RevisaoPendente(6, 1, "h", "odd fora da faixa", None, AGORA, None)
    ]
    assert total == 17
    sql = _sql(session.statements[0])
    assert "count(*) OVER ()" in sql
    assert "revisao_pendente.usuario_id = " in sql
    assert "revisao_pendente.resolvido_em IS NULL" in sql
    assert "revisao_pendente.motivo = " in sql
    assert "revisao_pendente.criado_em >= " in sql
    assert "revisao_pendente.criado_em < " in sql
    assert "ORDER BY revisao_pendente.criado_em, revisao_pendente.id" in sql
    assert "LIMIT %(param_1)s OFFSET %(param_2)s" in sql


async def test_revisao_pendente_page_keeps_total_after_the_last_page() -> None:
    class _EmptyPageSession(_Session):
        async def execute(self, statement: Any, params: Any = None) -> _Result:
            self.statements.append(statement)
            return _Result([] if len(self.statements) == 1 else [7])

    session = _EmptyPageSession()

    assert await RevisaoPendenteRepo().list_page(session, 1, {}, pagina=3, tamanho=10) == ([], 7)
    assert len(session.statements) == 2
    assert "FROM (SELECT" in _sql(session.statements[1])
    assert "LIMIT" not in _sql(session.statements[1])
    assert "OFFSET" not in _sql(session.statements[1])


async def test_revisao_pendente_stats_are_for_open_rows_of_one_tenant() -> None:
    session = _Session(
        SimpleNamespace(
            motivo="campo faltando",
            quantidade=2,
            mais_antiga_em=ONTEM,
            idade_maxima_segundos=86_400,
        ),
        SimpleNamespace(
            motivo="odd",
            quantidade=3,
            mais_antiga_em=AGORA - timedelta(hours=2),
            idade_maxima_segundos=7_200,
        ),
    )

    resultado = await RevisaoPendenteRepo().stats(session, 1)

    assert resultado == registros.EstatisticasRevisao(
        total=5,
        por_motivo={"campo faltando": 2, "odd": 3},
        mais_antiga_em=ONTEM,
        idade_maxima_segundos=86_400,
    )
    sql = _sql(session.statements[0])
    assert "revisao_pendente.usuario_id = " in sql
    assert "revisao_pendente.resolvido_em IS NULL" in sql
    assert "GROUP BY revisao_pendente.motivo" in sql
    assert "greatest(" in sql
    assert "EXTRACT(epoch FROM now() - min(revisao_pendente.criado_em))" in sql
    assert await RevisaoPendenteRepo().stats(_Session(), 1) == registros.EstatisticasRevisao(
        0, {}, None, 0
    )

    futura = _Session(
        SimpleNamespace(
            motivo="relógio adiantado",
            quantidade=1,
            mais_antiga_em=AGORA + timedelta(seconds=10),
            idade_maxima_segundos=-10,
        )
    )
    assert (await RevisaoPendenteRepo().stats(futura, 1)).idade_maxima_segundos == 0


async def test_revisao_photo_is_loaded_by_open_owned_review_not_by_raw_hash() -> None:
    session = _Session((b"jpeg", "image/jpeg"))

    assert await RevisaoPendenteRepo().get_foto_by_id(session, 1, 6) == registros.FotoRevisao(
        b"jpeg", "image/jpeg"
    )
    sql = _sql(session.statements[0])
    assert "FROM revisao_pendente JOIN midias" in sql
    assert "JOIN midia_arquivos" in sql
    assert "revisao_pendente.usuario_id = " in sql
    assert "revisao_pendente.id = " in sql
    assert "revisao_pendente.resolvido_em IS NULL" in sql
    assert await RevisaoPendenteRepo().get_foto_by_id(_Session(), 1, 6) is None


async def test_revisao_pendente_open_review_is_found_by_bet_and_reason() -> None:
    session = _Session(6)

    assert await RevisaoPendenteRepo().has_open(session, 1, "t:1:1:0", "odd") is True
    sql = _sql(session.statements[0])
    assert "revisao_pendente.extracao_bruta ->>" in sql
    assert "revisao_pendente.motivo = %(motivo_1)s" in sql
    assert "revisao_pendente.resolvido_em IS NULL" in sql
    assert await RevisaoPendenteRepo().has_open(_Session(), 1, "t:1:1:0", "odd") is False


async def test_revisao_pendente_superseded_reviews_of_a_bet_are_resolved() -> None:
    session = _Session(6, 7)

    assert await RevisaoPendenteRepo().resolve_superseded(session, 1, "t:1:1:0", "odd") == 2
    sql = _sql(session.statements[0])
    assert sql.startswith("UPDATE revisao_pendente SET resolvido_em=now()")
    assert "revisao_pendente.extracao_bruta ->>" in sql
    assert "revisao_pendente.resolvido_em IS NULL" in sql
    assert "revisao_pendente.motivo != %(motivo_1)s" in sql

    await RevisaoPendenteRepo().resolve_superseded(session, 1, "t:1:1:0", None)
    assert "motivo !=" not in _sql(session.statements[1])


async def test_aposta_upsert_materializada_only_lets_a_newer_write_in() -> None:
    session = _Session(_aposta())
    dados = {"usuario_id": 1, "chave": "t:1:1:0", "origem": "telegram", "odd": 1.9}

    aposta = await ApostaRepo().upsert_materializada(session, dados)

    assert aposta is not None
    assert aposta.chave == "t:1:1:0"
    sql = _sql(session.statements[0])
    assert "clock_timestamp()" in sql
    assert "DO UPDATE SET" in sql
    assert "odd = excluded.odd" in sql
    assert "atualizada_em = excluded.atualizada_em" in sql
    assert "usuario_id = excluded.usuario_id" not in sql
    assert "WHERE excluded.atualizada_em > apostas.atualizada_em" in sql
    assert await ApostaRepo().upsert_materializada(_Session(), dados) is None


async def test_conta_casa_is_found_by_the_house_name_and_the_bet_date() -> None:
    session = _Session(_conta())

    conta = await ContaCasaRepo().get_vigente_by_nome_da_casa(session, 1, "Betano")

    assert conta == registros.ContaCasa(3, 1, 2, "", ONTEM, None, True, estado=None)
    sql = _sql(session.statements[0])
    assert "JOIN casas ON casas.id = contas_casa.casa_id" in sql
    assert "casas.nome = %(nome_1)s" in sql
    assert "contas_casa.desde IS NULL" not in sql

    await ContaCasaRepo().get_vigente_by_nome_da_casa(session, 1, "Betano", AGORA)
    sql = _sql(session.statements[1])
    assert "contas_casa.desde IS NULL OR contas_casa.desde <=" in sql
    assert "contas_casa.ate IS NULL OR contas_casa.ate >=" in sql
    assert await ContaCasaRepo().get_vigente_by_nome_da_casa(_Session(), 1, "Betano") is None
