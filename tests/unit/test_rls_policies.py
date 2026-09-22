from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from bancaemdia.models import Base

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "be7d60cb5437"
RLS = "faf3ac7240ef"
TOKEN_LOOKUP = "3c1f0a9d7b21"
UPLOADS = "8f1c4a2b9d33"
SELECIONADA = "9c2d5e7f1a08"
TRANSFERENCIA = "a4e7d2c9f103"
IDEMPOTENCIA_CAIXA = "c7b1e9a42d60"
USUARIO_ATUAL = "NULLIF(current_setting('app.current_user_id', true), '')::bigint"
JOB_OFERECIDO = "NULLIF(current_setting('app.upload_job_id', true), '')::uuid"
POR_USUARIO = {
    "bancas",
    "contas_casa",
    "unidades",
    "movimentos",
    "apostas",
    "eventos",
    "revisao_pendente",
    "coletas_casa",
    "coleta_token",
    "chamadas_ia",
}
POR_USUARIO_UPLOAD = {
    "uploads",
    "upload_bilhetes",
    "upload_arquivos",
}
POR_USUARIO_IDEMPOTENCIA = {"movimento_requisicoes"}
COMPARTILHADAS = {
    "midia_arquivos",
    "casas",
    "esportes",
    "competicoes",
    "times",
    "mercados",
    "tipsters",
    "apelidos",
    "mensagens",
    "mensagem_versoes",
    "midias",
    "extracoes_cache",
}


def _config(buffer: io.StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _rls_revision() -> str:
    return RLS


def _upgrade_sql(alvo: str) -> str:
    buffer = io.StringIO()
    command.upgrade(_config(buffer), alvo, sql=True)
    return buffer.getvalue()


def _downgrade_sql(alvo: str) -> str:
    buffer = io.StringIO()
    command.downgrade(_config(buffer), alvo, sql=True)
    return buffer.getvalue()


def _policy(tabela: str) -> str:
    return (
        f"CREATE POLICY {tabela}_por_usuario ON {tabela} FOR ALL"
        f" USING (usuario_id = {USUARIO_ATUAL}) WITH CHECK (usuario_id = {USUARIO_ATUAL})"
    )


def test_rls_revision_follows_the_baseline() -> None:
    script = ScriptDirectory.from_config(_config())
    rls = script.get_revision(_rls_revision())
    assert rls.down_revision == BASELINE
    assert "002_rls_policies" in rls.doc
    assert script.get_base() == BASELINE


def test_protected_and_shared_tables_cover_the_whole_schema() -> None:
    protegidas = POR_USUARIO | POR_USUARIO_UPLOAD | POR_USUARIO_IDEMPOTENCIA
    assert protegidas | COMPARTILHADAS | {"usuarios"} == set(Base.metadata.tables)
    assert not protegidas & COMPARTILHADAS
    for tabela in protegidas:
        assert "usuario_id" in Base.metadata.tables[tabela].c
    for tabela in COMPARTILHADAS:
        assert "usuario_id" not in Base.metadata.tables[tabela].c


def test_every_per_user_table_is_protected() -> None:
    sql = _upgrade_sql(f"{BASELINE}:{_rls_revision()}")
    for tabela in POR_USUARIO:
        assert f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {tabela} FORCE ROW LEVEL SECURITY" in sql
        assert _policy(tabela) in sql
    assert sql.count("ENABLE ROW LEVEL SECURITY") == len(POR_USUARIO) + 1
    assert sql.count("CREATE POLICY ") == len(POR_USUARIO) + 4


def test_usuarios_can_be_read_and_created_but_only_changed_by_their_owner() -> None:
    sql = _upgrade_sql(f"{BASELINE}:{_rls_revision()}")
    assert "ALTER TABLE usuarios ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE usuarios FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY usuarios_leitura ON usuarios FOR SELECT USING (true)" in sql
    assert "CREATE POLICY usuarios_cadastro ON usuarios FOR INSERT WITH CHECK (true)" in sql
    assert (
        f"CREATE POLICY usuarios_alteracao ON usuarios FOR UPDATE USING (id = {USUARIO_ATUAL})"
        f" WITH CHECK (id = {USUARIO_ATUAL})"
    ) in sql
    assert (
        f"CREATE POLICY usuarios_exclusao ON usuarios FOR DELETE USING (id = {USUARIO_ATUAL})"
        in sql
    )
    assert "ON usuarios FOR ALL" not in sql


def test_shared_tables_stay_open() -> None:
    sql = _upgrade_sql(f"{BASELINE}:{_rls_revision()}")
    for tabela in COMPARTILHADAS:
        assert f"ALTER TABLE {tabela} " not in sql
        assert f"ON {tabela} " not in sql


def test_policy_reads_the_setting_the_session_sets() -> None:
    sql = _upgrade_sql(f"{BASELINE}:{_rls_revision()}")
    assert "current_setting('app.current_user_id', true)" in sql
    assert "::int)" not in sql
    assert "::bigint" in sql


def test_downgrade_removes_every_policy_and_disables_rls() -> None:
    sql = _downgrade_sql(f"{_rls_revision()}:{BASELINE}")
    for tabela in POR_USUARIO:
        assert f"DROP POLICY {tabela}_por_usuario ON {tabela}" in sql
        assert f"ALTER TABLE {tabela} NO FORCE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {tabela} DISABLE ROW LEVEL SECURITY" in sql
    for nome in (
        "usuarios_leitura",
        "usuarios_cadastro",
        "usuarios_alteracao",
        "usuarios_exclusao",
    ):
        assert f"DROP POLICY {nome} ON usuarios" in sql
    assert "ALTER TABLE usuarios DISABLE ROW LEVEL SECURITY" in sql
    assert sql.count("DROP POLICY ") == len(POR_USUARIO) + 4


def test_token_lookup_follows_the_rls_revision() -> None:
    script = ScriptDirectory.from_config(_config())

    assert script.get_revision(TOKEN_LOOKUP).down_revision == RLS


def test_uploads_revision_follows_the_token_lookup() -> None:
    script = ScriptDirectory.from_config(_config())

    assert script.get_revision(UPLOADS).down_revision == TOKEN_LOOKUP
    assert "004_uploads" in script.get_revision(UPLOADS).doc


def test_the_deleted_flag_revision_follows_the_uploads() -> None:
    script = ScriptDirectory.from_config(_config())

    assert script.get_revision(SELECIONADA).down_revision == UPLOADS
    assert "005_aposta_selecionada" in script.get_revision(SELECIONADA).doc


def test_the_transfer_revision_follows_the_deleted_flag() -> None:
    script = ScriptDirectory.from_config(_config())

    assert script.get_revision(TRANSFERENCIA).down_revision == SELECIONADA
    assert "006_movimento_transferencia" in script.get_revision(TRANSFERENCIA).doc


def test_cash_idempotency_follows_the_transfer_and_protects_its_table() -> None:
    script = ScriptDirectory.from_config(_config())

    assert script.get_current_head() == IDEMPOTENCIA_CAIXA
    assert script.get_revision(IDEMPOTENCIA_CAIXA).down_revision == TRANSFERENCIA
    assert "007_caixa_idempotencia" in script.get_revision(IDEMPOTENCIA_CAIXA).doc
    sql = _upgrade_sql(f"{TRANSFERENCIA}:{IDEMPOTENCIA_CAIXA}")
    tabela = "movimento_requisicoes"
    assert f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {tabela} FORCE ROW LEVEL SECURITY" in sql
    assert _policy(tabela) in sql
    assert "UNIQUE (usuario_id, chave_idempotencia)" in sql

    downgrade = _downgrade_sql(f"{IDEMPOTENCIA_CAIXA}:{TRANSFERENCIA}")
    assert f"DROP POLICY {tabela}_por_usuario ON {tabela}" in downgrade
    assert f"ALTER TABLE {tabela} DISABLE ROW LEVEL SECURITY" in downgrade
    assert f"DROP TABLE {tabela}" in downgrade


def test_every_upload_table_is_protected_by_user() -> None:
    sql = _upgrade_sql(f"{TOKEN_LOOKUP}:{UPLOADS}")

    for tabela in POR_USUARIO_UPLOAD:
        assert f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {tabela} FORCE ROW LEVEL SECURITY" in sql
        assert _policy(tabela) in sql
    assert "ALTER TABLE midia_arquivos ENABLE ROW LEVEL SECURITY" not in sql
    assert "ON midia_arquivos " not in sql
    assert sql.count("ENABLE ROW LEVEL SECURITY") == len(POR_USUARIO_UPLOAD)
    assert sql.count("CREATE POLICY ") == len(POR_USUARIO_UPLOAD) + 1


def test_uploads_are_readable_by_whoever_offers_the_job_id() -> None:
    sql = _upgrade_sql(f"{TOKEN_LOOKUP}:{UPLOADS}")

    assert (
        "CREATE POLICY uploads_por_job ON uploads FOR ALL"
        f" USING (job_id = {JOB_OFERECIDO}) WITH CHECK (job_id = {JOB_OFERECIDO})"
    ) in sql


def test_downgrade_drops_the_upload_policies() -> None:
    sql = _downgrade_sql(f"{UPLOADS}:{TOKEN_LOOKUP}")

    assert "DROP POLICY uploads_por_job ON uploads" in sql
    for tabela in POR_USUARIO_UPLOAD:
        assert f"DROP POLICY {tabela}_por_usuario ON {tabela}" in sql
        assert f"ALTER TABLE {tabela} DISABLE ROW LEVEL SECURITY" in sql
    assert sql.count("DROP POLICY ") == len(POR_USUARIO_UPLOAD) + 1


def test_coleta_token_is_readable_by_whoever_offers_its_hash() -> None:
    sql = _upgrade_sql(f"{RLS}:{TOKEN_LOOKUP}")

    assert (
        "CREATE POLICY coleta_token_por_hash ON coleta_token FOR SELECT"
        " USING (token_hash = NULLIF(current_setting('app.coleta_token_hash', true), ''))"
    ) in sql
    assert sql.count("CREATE POLICY ") == 1


def test_downgrade_drops_the_token_lookup_policy() -> None:
    sql = _downgrade_sql(f"{TOKEN_LOOKUP}:{RLS}")

    assert "DROP POLICY coleta_token_por_hash ON coleta_token" in sql
    assert sql.count("DROP POLICY ") == 1


def test_head_upgrade_chains_every_revision() -> None:
    sql = _upgrade_sql("head")
    rls = _rls_revision()
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{BASELINE}')" in sql
    assert (
        f"UPDATE alembic_version SET version_num='{rls}'"
        f" WHERE alembic_version.version_num = '{BASELINE}'"
    ) in sql
    assert sql.index("CREATE TABLE apostas (") < sql.index("CREATE POLICY apostas_por_usuario")
