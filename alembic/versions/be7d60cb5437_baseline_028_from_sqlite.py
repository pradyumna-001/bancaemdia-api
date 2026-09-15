"""baseline_028_from_sqlite

Revision ID: be7d60cb5437
Revises:
Create Date: 2026-09-05 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "be7d60cb5437"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARTICOES = """
DO $$
DECLARE
    tabela text;
    inicio date;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_partman') THEN
        CREATE SCHEMA IF NOT EXISTS partman;
        CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;
        PERFORM partman.create_parent(p_parent_table := 'public.eventos',
                                      p_control := 'criado_em', p_interval := '1 month', p_premake := 3);
        PERFORM partman.create_parent(p_parent_table := 'public.movimentos',
                                      p_control := 'ocorrido_em', p_interval := '1 month', p_premake := 3);
        PERFORM partman.create_parent(p_parent_table := 'public.mensagem_versoes',
                                      p_control := 'criado_em', p_interval := '1 month', p_premake := 3);
    ELSE
        FOREACH tabela IN ARRAY ARRAY['eventos', 'movimentos', 'mensagem_versoes'] LOOP
            EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF %I DEFAULT',
                           tabela || '_default', tabela);
            FOR inicio IN
                SELECT generate_series(date_trunc('month', now())::date,
                                       (date_trunc('month', now()) + interval '3 months')::date,
                                       interval '1 month')::date
            LOOP
                EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                               tabela || '_' || to_char(inicio, 'YYYY_MM'), tabela,
                               inicio, inicio + interval '1 month');
            END LOOP;
        END LOOP;
    END IF;
END $$;
"""

DESFAZER_PARTMAN = """
DO $$
BEGIN
    IF to_regclass('partman.part_config') IS NOT NULL THEN
        DELETE FROM partman.part_config
         WHERE parent_table IN ('public.eventos', 'public.movimentos', 'public.mensagem_versoes');
    END IF;
END $$;
"""


def upgrade() -> None:
    op.create_table(
        "apelidos",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("entidade_tipo", sa.String(), nullable=False),
        sa.Column("entidade_id", sa.BigInteger(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column("confirmado", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "entidade_tipo IN ('tipster', 'casa', 'time', 'mercado', 'esporte', 'competicao')",
            name="ck_apelidos_entidade_tipo",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entidade_tipo", "nome", name="uq_apelidos_nome"),
    )
    op.create_index(
        "idx_apelidos_busca", "apelidos", ["entidade_tipo", "entidade_id"], unique=False
    )
    op.create_table(
        "casas",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column("dominio", sa.String(), nullable=True),
        sa.Column("ativa", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dominio"),
        sa.UniqueConstraint("nome"),
    )
    op.create_table(
        "esportes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column("icone", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nome"),
    )
    op.create_table(
        "extracoes_cache",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("versao_prompt", sa.String(), nullable=False),
        sa.Column("resultado_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("chat_id", "message_id", "versao_prompt"),
    )
    op.create_table(
        "mensagens",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("autor_bruto", sa.String(), nullable=True),
        sa.Column("texto", sa.String(), nullable=False),
        sa.Column("data", sa.DateTime(timezone=True), nullable=False),
        sa.Column("versao_atual", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_mensagens_chat_message"),
    )
    op.create_index("idx_mensagens_autor", "mensagens", ["autor_bruto"], unique=False)
    op.create_index("idx_mensagens_data", "mensagens", ["data"], unique=False)
    op.create_table(
        "mercados",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column(
            "familia",
            sa.Enum(
                "GOLS",
                "CARTOES",
                "ESCANTEIOS",
                "RESULTADO",
                "HANDICAP",
                "JOGADOR",
                "AMBAS_MARCAM",
                "OUTRO",
                name="familia_de_mercado",
            ),
            server_default=sa.text("'OUTRO'"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nome"),
    )
    op.create_index("idx_mercados_familia", "mercados", ["familia"], unique=False)
    op.create_table(
        "times",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column(
            "apelidos",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nome"),
    )
    op.create_table(
        "tipsters",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column(
            "apelidos",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nome"),
    )
    op.create_table(
        "usuarios",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "usuarios_email_unico", "usuarios", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "bancas",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column("saldo_inicial_centavos", sa.BigInteger(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "saldo_inicial_centavos IS NULL OR saldo_inicial_centavos >= 0",
            name="ck_bancas_saldo_inicial",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usuario_id", "nome", name="uq_bancas_usuario_nome"),
    )
    op.create_table(
        "chamadas_ia",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=True),
        sa.Column("modelo", sa.String(), nullable=False),
        sa.Column("tokens_entrada", sa.Integer(), nullable=False),
        sa.Column("tokens_saida", sa.Integer(), nullable=False),
        sa.Column("custo_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_chamadas_data", "chamadas_ia", ["criado_em"], unique=False)
    op.create_index(
        "idx_chamadas_usuario", "chamadas_ia", ["usuario_id", "criado_em"], unique=False
    )
    op.create_table(
        "coleta_token",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("ativo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expira_em", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "idx_coleta_token_vivo",
        "coleta_token",
        ["usuario_id"],
        unique=True,
        postgresql_where=sa.text("ativo"),
    )
    op.create_table(
        "coletas_casa",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("casa_id", sa.BigInteger(), nullable=False),
        sa.Column("identidade", sa.String(), nullable=False),
        sa.Column("hash_conteudo", sa.String(), nullable=False),
        sa.Column("bruto_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "recebido_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processado_em", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["casa_id"],
            ["casas.id"],
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id", "casa_id", "identidade", name="uq_coletas_casa_identidade"
        ),
    )
    op.create_index("idx_coletas_usuario", "coletas_casa", ["usuario_id", "casa_id"], unique=False)
    op.create_table(
        "competicoes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("esporte_id", sa.BigInteger(), nullable=True),
        sa.Column("nome", sa.String(), nullable=False),
        sa.Column("pais", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["esporte_id"],
            ["esportes.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nome"),
    )
    op.create_table(
        "contas_casa",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("casa_id", sa.BigInteger(), nullable=False),
        sa.Column("apelido", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("desde", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ate", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ativa", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["casa_id"],
            ["casas.id"],
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id", "casa_id", "apelido", name="uq_contas_casa_usuario_casa_apelido"
        ),
    )
    op.create_index("idx_contas_casa_usuario", "contas_casa", ["usuario_id", "ativa"], unique=False)
    op.create_table(
        "eventos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("tipo", sa.String(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fonte", sa.String(), nullable=False),
        sa.Column("confianca", sa.Float(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("aposta_chave", sa.String(), nullable=True),
        sa.CheckConstraint(
            "fonte IN ('export', 'ia', 'manual', 'liquidacao', 'planilha', 'casa')",
            name="ck_eventos_fonte",
        ),
        sa.CheckConstraint(
            "tipo IN ('APOSTA_CRIADA', 'ODD_ALTERADA', 'STAKE_ALTERADA', 'RESULTADO_REGISTRADO', 'CASHOUT_REGISTRADO', 'APOSTA_ANULADA', 'APOSTA_CANCELADA', 'SELECAO_ALTERADA', 'CORRECAO_MANUAL', 'MOVIMENTO_REGISTRADO', 'CLV_REGISTRADO')",
            name="ck_eventos_tipo",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id", "criado_em"),
        postgresql_partition_by="RANGE (criado_em)",
    )
    op.create_index(
        "idx_eventos_chave", "eventos", ["usuario_id", "aposta_chave", "id"], unique=False
    )
    op.create_index("idx_eventos_usuario", "eventos", ["usuario_id", "criado_em"], unique=False)
    op.create_table(
        "mensagem_versoes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("mensagem_id", sa.BigInteger(), nullable=False),
        sa.Column("texto", sa.String(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["mensagem_id"],
            ["mensagens.id"],
        ),
        sa.PrimaryKeyConstraint("id", "criado_em"),
        postgresql_partition_by="RANGE (criado_em)",
    )
    op.create_index("idx_versoes_mensagem", "mensagem_versoes", ["mensagem_id"], unique=False)
    op.create_table(
        "midias",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("mensagem_id", sa.BigInteger(), nullable=True),
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("tipo", sa.String(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["mensagem_id"],
            ["mensagens.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    op.create_index("idx_midias_mensagem", "midias", ["mensagem_id"], unique=False)
    op.create_table(
        "revisao_pendente",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("midia_hash", sa.String(), nullable=True),
        sa.Column("motivo", sa.String(), nullable=False),
        sa.Column("extracao_bruta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("resolvido_em", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_revisao_criado", "revisao_pendente", ["criado_em"], unique=False)
    op.create_index(
        "idx_revisao_usuario", "revisao_pendente", ["usuario_id", "resolvido_em"], unique=False
    )
    op.create_table(
        "unidades",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("valor_centavos", sa.BigInteger(), nullable=False),
        sa.Column("vigente_de", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vigente_ate", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("valor_centavos > 0", name="ck_unidades_valor"),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_unidades_usuario", "unidades", ["usuario_id", "vigente_de"], unique=False)
    op.create_table(
        "apostas",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("chave", sa.String(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("ordem_na_mensagem", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("midia_hash", sa.String(), nullable=True),
        sa.Column("banca_id", sa.BigInteger(), nullable=True),
        sa.Column("conta_casa_id", sa.BigInteger(), nullable=True),
        sa.Column("tipster_id", sa.BigInteger(), nullable=True),
        sa.Column("time_casa_id", sa.BigInteger(), nullable=True),
        sa.Column("time_fora_id", sa.BigInteger(), nullable=True),
        sa.Column("mercado_id", sa.BigInteger(), nullable=True),
        sa.Column("competicao_id", sa.BigInteger(), nullable=True),
        sa.Column("data_aposta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_jogo", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stake_unidades", sa.Float(), nullable=False),
        sa.Column("stake_centavos", sa.BigInteger(), nullable=False),
        sa.Column(
            "valor_aposta_centavos", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("odd", sa.Float(), nullable=True),
        sa.Column("retorno_centavos", sa.BigInteger(), nullable=True),
        sa.Column("estado", sa.String(), server_default=sa.text("'PENDENTE'"), nullable=False),
        sa.Column("origem", sa.String(), nullable=False),
        sa.Column("freebet", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("duvida_de_par", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("parceira_chave", sa.String(), nullable=True),
        sa.Column("duplicada_de", sa.String(), nullable=True),
        sa.Column("revisao_grave", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "criada_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "atualizada_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "estado IN ('PENDENTE', 'GREEN', 'RED', 'ANULADA', 'MEIO_GREEN', 'MEIO_RED', 'CASHOUT')",
            name="ck_apostas_estado",
        ),
        sa.CheckConstraint(
            "origem <> 'telegram' OR (chat_id IS NOT NULL AND message_id IS NOT NULL)",
            name="ck_apostas_telegram_tem_mensagem",
        ),
        sa.CheckConstraint(
            "origem IN ('telegram', 'print', 'manual', 'planilha', 'casa')",
            name="ck_apostas_origem",
        ),
        sa.CheckConstraint("NOT freebet OR stake_centavos = 0", name="ck_apostas_freebet"),
        sa.CheckConstraint("stake_centavos >= 0", name="ck_apostas_stake"),
        sa.ForeignKeyConstraint(
            ["banca_id"],
            ["bancas.id"],
        ),
        sa.ForeignKeyConstraint(
            ["competicao_id"],
            ["competicoes.id"],
        ),
        sa.ForeignKeyConstraint(
            ["conta_casa_id"],
            ["contas_casa.id"],
        ),
        sa.ForeignKeyConstraint(
            ["mercado_id"],
            ["mercados.id"],
        ),
        sa.ForeignKeyConstraint(
            ["time_casa_id"],
            ["times.id"],
        ),
        sa.ForeignKeyConstraint(
            ["time_fora_id"],
            ["times.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tipster_id"],
            ["tipsters.id"],
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "usuario_id",
            "chat_id",
            "message_id",
            "ordem_na_mensagem",
            name="uq_apostas_mensagem_ordem",
        ),
    )
    op.create_index("idx_apostas_banca", "apostas", ["banca_id", "criada_em"], unique=False)
    op.create_index(
        "idx_apostas_chave",
        "apostas",
        ["usuario_id", "chave"],
        unique=True,
        postgresql_where=sa.text("chave IS NOT NULL"),
    )
    op.create_index(
        "idx_apostas_competicao", "apostas", ["usuario_id", "competicao_id"], unique=False
    )
    op.create_index("idx_apostas_data", "apostas", ["usuario_id", "data_aposta"], unique=False)
    op.create_index(
        "idx_apostas_duplicada", "apostas", ["usuario_id", "duplicada_de"], unique=False
    )
    op.create_index("idx_apostas_estado", "apostas", ["usuario_id", "estado"], unique=False)
    op.create_index("idx_apostas_mensagem", "apostas", ["chat_id", "message_id"], unique=False)
    op.create_index("idx_apostas_mercado", "apostas", ["mercado_id"], unique=False)
    op.create_index("idx_apostas_origem", "apostas", ["usuario_id", "origem"], unique=False)
    op.create_index(
        "idx_apostas_revisao",
        "apostas",
        ["usuario_id", "revisao_grave"],
        unique=False,
        postgresql_where=sa.text("revisao_grave"),
    )
    op.create_index("idx_apostas_time", "apostas", ["time_casa_id"], unique=False)
    op.create_index("idx_apostas_usuario", "apostas", ["usuario_id", "criada_em"], unique=False)
    op.create_table(
        "movimentos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("usuario_id", sa.BigInteger(), nullable=False),
        sa.Column("conta_casa_id", sa.BigInteger(), nullable=True),
        sa.Column("tipo", sa.String(), nullable=False),
        sa.Column("valor_centavos", sa.BigInteger(), nullable=False),
        sa.Column("ocorrido_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("descricao", sa.String(), nullable=True),
        sa.CheckConstraint(
            "tipo IN ('DEPOSITO', 'SAQUE', 'TRANSFERENCIA', 'BONUS', 'AJUSTE')",
            name="ck_movimentos_tipo",
        ),
        sa.CheckConstraint("valor_centavos <> 0", name="ck_movimentos_valor"),
        sa.ForeignKeyConstraint(
            ["conta_casa_id"],
            ["contas_casa.id"],
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
        ),
        sa.PrimaryKeyConstraint("id", "ocorrido_em"),
        postgresql_partition_by="RANGE (ocorrido_em)",
    )
    op.create_index(
        "idx_movimentos_conta", "movimentos", ["conta_casa_id", "ocorrido_em"], unique=False
    )
    op.create_index(
        "idx_movimentos_usuario", "movimentos", ["usuario_id", "ocorrido_em"], unique=False
    )
    op.execute(PARTICOES)


def downgrade() -> None:
    op.execute(DESFAZER_PARTMAN)
    op.drop_table("movimentos")
    op.drop_table("apostas")
    op.drop_table("unidades")
    op.drop_table("revisao_pendente")
    op.drop_table("midias")
    op.drop_table("mensagem_versoes")
    op.drop_table("eventos")
    op.drop_table("contas_casa")
    op.drop_table("competicoes")
    op.drop_table("coletas_casa")
    op.drop_table("coleta_token")
    op.drop_table("chamadas_ia")
    op.drop_table("bancas")
    op.drop_table("usuarios")
    op.drop_table("tipsters")
    op.drop_table("times")
    op.drop_table("mercados")
    op.drop_table("mensagens")
    op.drop_table("extracoes_cache")
    op.drop_table("esportes")
    op.drop_table("casas")
    op.drop_table("apelidos")
    op.execute("DROP TYPE familia_de_mercado")
