from __future__ import annotations

from sqlalchemy import BigInteger, DateTime, create_mock_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.ddl import DDLElement

from bancaemdia.db.models import Base
from bancaemdia.models.aposta import ESTADOS, ORIGENS, Aposta
from bancaemdia.models.banca import Banca
from bancaemdia.models.conta_casa import ContaCasa
from bancaemdia.models.evento import FONTES, TIPOS_DE_EVENTO, Evento
from bancaemdia.models.movimento import TIPOS_DE_MOVIMENTO, Movimento
from bancaemdia.models.unidade import Unidade
from bancaemdia.models.usuario import Usuario

MODELOS = (Usuario, Banca, ContaCasa, Unidade, Movimento, Aposta, Evento)
TABELAS = {"usuarios", "bancas", "contas_casa", "unidades", "movimentos", "apostas", "eventos"}
TEMPORAIS = {
    "usuarios": ("criado_em",),
    "bancas": ("criado_em",),
    "contas_casa": ("desde", "ate"),
    "unidades": ("vigente_de", "vigente_ate"),
    "movimentos": ("ocorrido_em",),
    "apostas": ("data_aposta", "data_jogo", "criada_em", "atualizada_em"),
    "eventos": ("criado_em",),
}


def _ddl() -> str:
    statements: list[str] = []

    def executor(sql: DDLElement, *args: object, **kwargs: object) -> None:
        statements.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine("postgresql+asyncpg://", executor)
    Base.metadata.create_all(engine, checkfirst=False)
    return "\n".join(statements)


def _create_table(nome: str) -> str:
    ddl = _ddl()
    inicio = ddl.index(f"CREATE TABLE {nome} (")
    return ddl[inicio : ddl.index("\n\n", inicio)]


def _fk_targets(modelo: type[Base]) -> dict[str, str]:
    return {fk.parent.name: fk.target_fullname for fk in modelo.__table__.foreign_keys}


def test_seven_core_tables_are_registered() -> None:
    assert TABELAS <= set(Base.metadata.tables)
    assert {m.__tablename__ for m in MODELOS} == TABELAS


def test_create_all_emits_one_create_table_per_model() -> None:
    ddl = _ddl()
    for nome in TABELAS:
        assert ddl.count(f"CREATE TABLE {nome} (") == 1


def test_primary_keys_are_bigserial() -> None:
    for nome in TABELAS:
        assert "id BIGSERIAL NOT NULL" in _create_table(nome)


def test_partitioned_tables_carry_the_partition_key_in_the_primary_key() -> None:
    assert [c.name for c in Evento.__table__.primary_key.columns] == ["id", "criado_em"]
    assert [c.name for c in Movimento.__table__.primary_key.columns] == ["id", "ocorrido_em"]
    assert "PARTITION BY RANGE (criado_em)" in _create_table("eventos")
    assert "PARTITION BY RANGE (ocorrido_em)" in _create_table("movimentos")


def test_other_tables_are_not_partitioned() -> None:
    for nome in TABELAS - {"eventos", "movimentos"}:
        assert "PARTITION BY" not in _create_table(nome)
        assert [c.name for c in Base.metadata.tables[nome].primary_key.columns] == ["id"]


def test_every_centavos_column_is_bigint() -> None:
    colunas = [
        c
        for nome in TABELAS
        for c in Base.metadata.tables[nome].columns
        if c.name.endswith("_centavos")
    ]
    assert len(colunas) == 6
    assert all(isinstance(c.type, BigInteger) for c in colunas)


def test_temporal_columns_are_timestamptz() -> None:
    for nome, colunas in TEMPORAIS.items():
        for coluna in colunas:
            tipo = Base.metadata.tables[nome].c[coluna].type
            assert isinstance(tipo, DateTime)
            assert tipo.timezone is True
    assert _create_table("unidades").count("TIMESTAMP WITH TIME ZONE") == 2


def test_payload_is_jsonb() -> None:
    assert isinstance(Evento.__table__.c.payload_json.type, JSONB)
    assert "payload_json JSONB NOT NULL" in _create_table("eventos")


def test_every_per_user_table_points_to_usuarios() -> None:
    for modelo in MODELOS[1:]:
        assert _fk_targets(modelo)["usuario_id"] == "usuarios.id"
        assert modelo.__table__.c.usuario_id.nullable is False


def test_foreign_keys_between_core_tables() -> None:
    assert _fk_targets(Aposta)["banca_id"] == "bancas.id"
    assert _fk_targets(Aposta)["conta_casa_id"] == "contas_casa.id"
    assert _fk_targets(Movimento)["conta_casa_id"] == "contas_casa.id"


def test_foreign_keys_to_canonical_tables() -> None:
    assert _fk_targets(ContaCasa)["casa_id"] == "casas.id"
    alvos = _fk_targets(Aposta)
    assert alvos["tipster_id"] == "tipsters.id"
    assert alvos["time_casa_id"] == "times.id"
    assert alvos["time_fora_id"] == "times.id"
    assert alvos["mercado_id"] == "mercados.id"
    assert alvos["competicao_id"] == "competicoes.id"


def test_apostas_check_constraints() -> None:
    ddl = _create_table("apostas")
    assert "CHECK (stake_centavos >= 0)" in ddl
    assert "CHECK (NOT freebet OR stake_centavos = 0)" in ddl
    assert "CHECK (origem <> 'telegram' OR (chat_id IS NOT NULL AND message_id IS NOT NULL))" in ddl
    assert "CHECK (estado IN ('PENDENTE', 'GREEN', 'RED', 'ANULADA', 'MEIO_GREEN'" in ddl
    assert "CHECK (origem IN ('telegram', 'print', 'manual', 'planilha', 'casa'))" in ddl


def test_allowed_values_match_the_original_schema() -> None:
    assert ESTADOS == ("PENDENTE", "GREEN", "RED", "ANULADA", "MEIO_GREEN", "MEIO_RED", "CASHOUT")
    assert ORIGENS == ("telegram", "print", "manual", "planilha", "casa")
    assert TIPOS_DE_MOVIMENTO == ("DEPOSITO", "SAQUE", "TRANSFERENCIA", "BONUS", "AJUSTE")
    assert FONTES == ("export", "ia", "manual", "liquidacao", "planilha", "casa")
    assert len(TIPOS_DE_EVENTO) == 11
    assert "APOSTA_CRIADA" in TIPOS_DE_EVENTO


def test_other_check_constraints() -> None:
    assert "CHECK (valor_centavos > 0)" in _create_table("unidades")
    assert "CHECK (valor_centavos <> 0)" in _create_table("movimentos")
    assert "CHECK (tipo IN ('DEPOSITO', 'SAQUE'" in _create_table("movimentos")
    assert "CHECK (tipo IN ('APOSTA_CRIADA'" in _create_table("eventos")
    assert "CHECK (fonte IN ('export', 'ia'" in _create_table("eventos")
    assert "saldo_inicial_centavos IS NULL OR saldo_inicial_centavos >= 0" in _create_table(
        "bancas"
    )


def test_apostas_indexes_from_adr_002() -> None:
    indices = {i.name: [c.name for c in i.columns] for i in Aposta.__table__.indexes}
    assert indices == {
        "idx_apostas_usuario": ["usuario_id", "criada_em"],
        "idx_apostas_estado": ["usuario_id", "estado"],
        "idx_apostas_banca": ["banca_id", "criada_em"],
        "idx_apostas_origem": ["usuario_id", "origem"],
        "idx_apostas_mensagem": ["chat_id", "message_id"],
        "idx_apostas_mercado": ["mercado_id"],
        "idx_apostas_time": ["time_casa_id"],
        "idx_apostas_competicao": ["usuario_id", "competicao_id"],
        "idx_apostas_data": ["usuario_id", "data_aposta"],
        "idx_apostas_duplicada": ["usuario_id", "duplicada_de"],
        "idx_apostas_revisao": ["usuario_id", "revisao_grave"],
        "idx_apostas_apagadas": ["usuario_id", "criada_em"],
        "idx_apostas_chave": ["usuario_id", "chave"],
    }


def test_partial_and_unique_indexes_render_their_where() -> None:
    ddl = _ddl()
    assert (
        "CREATE UNIQUE INDEX idx_apostas_chave ON apostas (usuario_id, chave)"
        " WHERE chave IS NOT NULL"
    ) in ddl
    assert (
        "CREATE INDEX idx_apostas_revisao ON apostas (usuario_id, revisao_grave)"
        " WHERE revisao_grave"
    ) in ddl
    assert "CREATE UNIQUE INDEX usuarios_email_unico ON usuarios (lower(email))" in ddl


def test_event_and_ledger_indexes() -> None:
    eventos = {i.name: [c.name for c in i.columns] for i in Evento.__table__.indexes}
    assert eventos == {
        "idx_eventos_usuario": ["usuario_id", "criado_em"],
        "idx_eventos_chave": ["usuario_id", "aposta_chave", "id"],
    }
    movimentos = {i.name: [c.name for c in i.columns] for i in Movimento.__table__.indexes}
    assert movimentos == {
        "idx_movimentos_usuario": ["usuario_id", "ocorrido_em"],
        "idx_movimentos_conta": ["conta_casa_id", "ocorrido_em"],
    }
    unidades = {i.name: [c.name for c in i.columns] for i in Unidade.__table__.indexes}
    assert unidades == {"idx_unidades_usuario": ["usuario_id", "vigente_de"]}
    contas = {i.name: [c.name for c in i.columns] for i in ContaCasa.__table__.indexes}
    assert contas == {"idx_contas_casa_usuario": ["usuario_id", "ativa"]}


def test_unique_constraints_from_the_original_schema() -> None:
    ddl = _ddl()
    assert (
        "CONSTRAINT uq_apostas_mensagem_ordem"
        " UNIQUE (usuario_id, chat_id, message_id, ordem_na_mensagem)"
    ) in ddl
    assert "CONSTRAINT uq_bancas_usuario_nome UNIQUE (usuario_id, nome)" in ddl
    assert (
        "CONSTRAINT uq_contas_casa_usuario_casa_apelido UNIQUE (usuario_id, casa_id, apelido)"
    ) in ddl


def test_server_defaults() -> None:
    apostas = _create_table("apostas")
    assert "estado VARCHAR DEFAULT 'PENDENTE' NOT NULL" in apostas
    assert "freebet BOOLEAN DEFAULT false NOT NULL" in apostas
    assert "revisao_grave BOOLEAN DEFAULT false NOT NULL" in apostas
    assert "ordem_na_mensagem INTEGER DEFAULT 0 NOT NULL" in apostas
    assert "criada_em TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in apostas
    assert "ativa BOOLEAN DEFAULT true NOT NULL" in _create_table("contas_casa")
    assert "apelido VARCHAR DEFAULT '' NOT NULL" in _create_table("contas_casa")
    assert "ativo BOOLEAN DEFAULT true NOT NULL" in _create_table("usuarios")


def test_atualizada_em_refreshes_on_update() -> None:
    assert Aposta.__table__.c.atualizada_em.onupdate is not None
    assert Aposta.__table__.c.criada_em.onupdate is None


def test_nullability_follows_the_issue() -> None:
    apostas = Aposta.__table__.c
    assert all(apostas[c].nullable is False for c in ("stake_unidades", "stake_centavos", "origem"))
    assert all(
        apostas[c].nullable is True for c in ("chave", "odd", "retorno_centavos", "banca_id")
    )
    assert Movimento.__table__.c.conta_casa_id.nullable is True
    assert Unidade.__table__.c.vigente_ate.nullable is True
    assert Evento.__table__.c.aposta_chave.nullable is True
