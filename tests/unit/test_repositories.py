from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from bancaemdia import models
from bancaemdia.domain import registros
from bancaemdia.repositories.aposta_repo import ApostaRepo
from bancaemdia.repositories.base import colunas
from bancaemdia.repositories.coleta_casa_repo import ColetaCasaRepo
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


class _Session:
    def __init__(self, *objs: Any) -> None:
        self.objs = list(objs)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
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
    assert conta == registros.ContaCasa(3, 1, 2, "", ONTEM, None, True)
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
