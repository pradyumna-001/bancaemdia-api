from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from bancaemdia.models import Base

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "be7d60cb5437"
HEAD = "a4e7d2c9f103"
PARTICIONADAS = {
    "eventos": "criado_em",
    "movimentos": "ocorrido_em",
    "mensagem_versoes": "criado_em",
}


def _config(buffer: io.StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _upgrade_sql() -> str:
    buffer = io.StringIO()
    command.upgrade(_config(buffer), "head", sql=True)
    return buffer.getvalue()


def _downgrade_sql() -> str:
    buffer = io.StringIO()
    command.downgrade(_config(buffer), f"{HEAD}:base", sql=True)
    return buffer.getvalue()


def test_the_baseline_is_the_root_revision() -> None:
    script = ScriptDirectory.from_config(_config())
    assert script.get_base() == BASELINE
    baseline = script.get_revision(BASELINE)
    assert baseline.down_revision is None
    assert "baseline_028_from_sqlite" in baseline.doc


def test_upgrade_creates_every_model_table_once() -> None:
    sql = _upgrade_sql()
    for nome in Base.metadata.tables:
        assert sql.count(f"\nCREATE TABLE {nome} (") == 1
    assert sql.count("\nCREATE TABLE ") == len(Base.metadata.tables) + 1
    assert "CREATE TABLE alembic_version" in sql
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{BASELINE}')" in sql


def test_upgrade_renders_the_schema_of_the_models() -> None:
    sql = _upgrade_sql()
    assert sql.count("id BIGSERIAL NOT NULL") == len(Base.metadata.tables) - 1
    assert "CREATE TYPE familia_de_mercado AS ENUM ('GOLS'" in sql
    assert "payload_json JSONB NOT NULL" in sql
    assert "CHECK (NOT freebet OR stake_centavos = 0)" in sql
    assert "CHECK (origem <> 'telegram' OR (chat_id IS NOT NULL AND message_id IS NOT NULL))" in sql
    assert (
        "CREATE UNIQUE INDEX idx_apostas_chave ON apostas (usuario_id, chave)"
        " WHERE chave IS NOT NULL"
    ) in sql
    assert "CREATE UNIQUE INDEX usuarios_email_unico ON usuarios (lower(email))" in sql
    assert (
        "CREATE UNIQUE INDEX idx_coleta_token_vivo ON coleta_token (usuario_id) WHERE ativo" in sql
    )
    assert "FOREIGN KEY(casa_id) REFERENCES casas (id)" in sql


def test_upgrade_creates_every_index_of_the_models() -> None:
    sql = _upgrade_sql()
    for tabela in Base.metadata.tables.values():
        for indice in tabela.indexes:
            assert f"INDEX {indice.name} ON {tabela.name} (" in sql


def test_partitioned_tables_get_their_monthly_partitions() -> None:
    sql = _upgrade_sql()
    for tabela, coluna in PARTICIONADAS.items():
        assert f"CREATE TABLE {tabela} (" in sql
        assert f"PARTITION BY RANGE ({coluna})" in sql
        assert f"p_parent_table := 'public.{tabela}'" in sql
    assert sql.count("PARTITION BY RANGE") == 3
    assert sql.count("partman.create_parent(") == 3
    assert "p_premake := 3" in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_partman" in sql
    assert "ARRAY['eventos', 'movimentos', 'mensagem_versoes']" in sql
    assert "PARTITION OF %I DEFAULT" in sql
    assert "interval '3 months'" in sql


def test_downgrade_removes_everything_the_upgrade_created() -> None:
    sql = _downgrade_sql()
    for nome in Base.metadata.tables:
        assert f"DROP TABLE {nome}" in sql
    assert "DROP TYPE familia_de_mercado" in sql
    assert "DELETE FROM partman.part_config" in sql
    assert f"DELETE FROM alembic_version WHERE alembic_version.version_num = '{BASELINE}'" in sql


def test_tables_are_created_in_dependency_order() -> None:
    sql = _upgrade_sql()
    assert sql.index("CREATE TABLE usuarios (") < sql.index("CREATE TABLE apostas (")
    assert sql.index("CREATE TABLE casas (") < sql.index("CREATE TABLE contas_casa (")
    assert sql.index("CREATE TABLE contas_casa (") < sql.index("CREATE TABLE apostas (")
    assert sql.index("CREATE TABLE mensagens (") < sql.index("CREATE TABLE midias (")
    assert sql.index("CREATE TABLE apostas (") < sql.index("DO $$")
