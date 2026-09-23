from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from bancaemdia.models import Base

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "be7d60cb5437"
TRANSFERENCIA = "a4e7d2c9f103"
IDEMPOTENCIA_CAIXA = "c7b1e9a42d60"
REVISAO_RESOLVIDA = "f2a9c4e7b106"
CAIXA_REVISAO_MERGE = "e5a1c7d9b204"
PAINEL = "d3f6a8c1e209"
PAINEL_CAIXA_MERGE = "f8b2d4a6c901"
HEAD = "a9d6e3f1c210"
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


def test_late_branches_merge_without_rewriting_published_revisions() -> None:
    script = ScriptDirectory.from_config(_config())
    revisao = script.get_revision(REVISAO_RESOLVIDA)
    caixa_revisao = script.get_revision(CAIXA_REVISAO_MERGE)
    painel = script.get_revision(PAINEL)
    merge = script.get_revision(PAINEL_CAIXA_MERGE)

    assert script.get_current_head() == HEAD
    assert script.get_revision(IDEMPOTENCIA_CAIXA).down_revision == TRANSFERENCIA
    assert revisao.down_revision == TRANSFERENCIA
    assert "007_revisao_resolvida_evento" in revisao.doc
    assert set(caixa_revisao.down_revision) == {IDEMPOTENCIA_CAIXA, REVISAO_RESOLVIDA}
    assert "008_merge_caixa_revisao" in caixa_revisao.doc
    assert painel.down_revision == REVISAO_RESOLVIDA
    assert "008_painel_materialized_views" in painel.doc
    assert set(merge.down_revision) == {CAIXA_REVISAO_MERGE, PAINEL}
    assert "009_merge_painel_caixa" in merge.doc

    upgrade = _upgrade_sql()
    assert "ALTER TABLE eventos DROP CONSTRAINT ck_eventos_tipo" in upgrade
    assert "REVISAO_RESOLVIDA" in upgrade

    downgrade = _downgrade_sql()
    regra_compativel = downgrade.index("ALTER TABLE eventos ADD CONSTRAINT ck_eventos_tipo")
    # O rollback mantém a auditoria legível e não falha depois do primeiro uso.
    assert "REVISAO_RESOLVIDA" in downgrade[regra_compativel : regra_compativel + 500]


def test_upgrade_creates_every_model_table_once() -> None:
    sql = _upgrade_sql()
    for nome in Base.metadata.tables:
        assert sql.count(f"\nCREATE TABLE {nome} (") == 1
    # Alembic's own version table and the private dashboard refresh state are intentionally not
    # SQLAlchemy models.
    assert sql.count("\nCREATE TABLE ") == len(Base.metadata.tables) + 2
    assert "CREATE TABLE alembic_version" in sql
    assert "CREATE TABLE painel.estado_refresh" in sql
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
    assert "DROP TABLE IF EXISTS painel.estado_refresh" in sql
    assert "DROP SCHEMA IF EXISTS painel" in sql
    assert "DELETE FROM partman.part_config" in sql
    assert f"DELETE FROM alembic_version WHERE alembic_version.version_num = '{BASELINE}'" in sql


def test_tables_are_created_in_dependency_order() -> None:
    sql = _upgrade_sql()
    assert sql.index("CREATE TABLE usuarios (") < sql.index("CREATE TABLE apostas (")
    assert sql.index("CREATE TABLE casas (") < sql.index("CREATE TABLE contas_casa (")
    assert sql.index("CREATE TABLE contas_casa (") < sql.index("CREATE TABLE apostas (")
    assert sql.index("CREATE TABLE mensagens (") < sql.index("CREATE TABLE midias (")
    assert sql.index("CREATE TABLE apostas (") < sql.index("DO $$")
