from __future__ import annotations

from sqlalchemy import Enum, Numeric, create_mock_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.ddl import DDLElement

from bancaemdia.db.models import Base
from bancaemdia.models.apelido import ENTIDADES, Apelido
from bancaemdia.models.aposta import Aposta
from bancaemdia.models.assinatura import Assinatura
from bancaemdia.models.audit_log import AuditLog
from bancaemdia.models.banca import Banca
from bancaemdia.models.billing_price import BillingPrice
from bancaemdia.models.billing_price_audit import BillingPriceAudit
from bancaemdia.models.billing_rollout import BillingRollout
from bancaemdia.models.casa import Casa
from bancaemdia.models.chamada_ia import ChamadaIA
from bancaemdia.models.coleta_casa import ColetaCasa
from bancaemdia.models.coleta_token import ColetaToken
from bancaemdia.models.competicao import Competicao
from bancaemdia.models.conta_casa import ContaCasa
from bancaemdia.models.esporte import Esporte
from bancaemdia.models.evento import Evento
from bancaemdia.models.extracao_cache import ExtracaoCache
from bancaemdia.models.mensagem import Mensagem
from bancaemdia.models.mensagem_versao import MensagemVersao
from bancaemdia.models.mercado import FAMILIAS, Mercado
from bancaemdia.models.midia import Midia
from bancaemdia.models.midia_arquivo import MidiaArquivo
from bancaemdia.models.movimento import Movimento
from bancaemdia.models.movimento_requisicao import MovimentoRequisicao
from bancaemdia.models.revisao_pendente import RevisaoPendente
from bancaemdia.models.time import Time
from bancaemdia.models.tipster import Tipster
from bancaemdia.models.titular import Titular
from bancaemdia.models.troca_titular import TrocaTitularEvento, TrocaTitularRequisicao
from bancaemdia.models.unidade import Unidade
from bancaemdia.models.upload import Upload, UploadArquivo, UploadBilhete
from bancaemdia.models.uso_conta_casa import UsoContaCasa
from bancaemdia.models.usuario import Usuario

NUCLEO = (Usuario, Banca, ContaCasa, Unidade, Movimento, Aposta, Evento)
CANONICOS = (Casa, Esporte, Competicao, Time, Mercado, Tipster, Apelido)
SUPORTE = (
    Mensagem,
    MensagemVersao,
    Midia,
    ExtracaoCache,
    ChamadaIA,
    ColetaCasa,
    ColetaToken,
    RevisaoPendente,
    MidiaArquivo,
    MovimentoRequisicao,
)
UPLOAD = (Upload, UploadBilhete, UploadArquivo)
BILLING = (Assinatura, BillingPrice, BillingPriceAudit, BillingRollout)
HOLDERS = (Titular, UsoContaCasa, TrocaTitularEvento, TrocaTitularRequisicao)
POR_USUARIO = (
    ChamadaIA,
    ColetaCasa,
    ColetaToken,
    RevisaoPendente,
    Upload,
    UploadBilhete,
    UploadArquivo,
    MovimentoRequisicao,
)


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


def _indexes(modelo: type[Base]) -> dict[str, list[str]]:
    return {i.name: [c.name for c in i.columns] for i in modelo.__table__.indexes}


def test_all_thirty_six_tables_are_registered() -> None:
    esperadas = {
        m.__tablename__
        for m in NUCLEO + CANONICOS + SUPORTE + UPLOAD + BILLING + HOLDERS + (AuditLog,)
    }
    assert len(esperadas) == 36
    assert set(Base.metadata.tables) == esperadas
    assert {m.__tablename__ for m in CANONICOS} == {
        "casas",
        "esportes",
        "competicoes",
        "times",
        "mercados",
        "tipsters",
        "apelidos",
    }
    assert {m.__tablename__ for m in SUPORTE} == {
        "mensagens",
        "mensagem_versoes",
        "midias",
        "extracoes_cache",
        "chamadas_ia",
        "coletas_casa",
        "coleta_token",
        "revisao_pendente",
        "midia_arquivos",
        "movimento_requisicoes",
    }
    assert {m.__tablename__ for m in UPLOAD} == {"uploads", "upload_bilhetes", "upload_arquivos"}


def test_create_all_renders_every_table_once() -> None:
    ddl = _ddl()
    for nome in Base.metadata.tables:
        assert ddl.count(f"CREATE TABLE {nome} (") == 1


def test_canonical_tables_have_no_usuario_id() -> None:
    for modelo in CANONICOS:
        assert "usuario_id" not in modelo.__table__.c


def test_per_user_support_tables_point_to_usuarios() -> None:
    for modelo in POR_USUARIO:
        assert _fk_targets(modelo)["usuario_id"] == "usuarios.id"
    assert ChamadaIA.__table__.c.usuario_id.nullable is True
    assert RevisaoPendente.__table__.c.usuario_id.nullable is False


def test_foreign_keys_match_appendix_a() -> None:
    assert _fk_targets(Competicao)["esporte_id"] == "esportes.id"
    assert _fk_targets(ColetaCasa)["casa_id"] == "casas.id"
    assert _fk_targets(MensagemVersao)["mensagem_id"] == "mensagens.id"
    assert _fk_targets(Midia)["mensagem_id"] == "mensagens.id"
    assert Midia.__table__.c.mensagem_id.nullable is True
    assert Competicao.__table__.c.esporte_id.nullable is True


def test_canonical_names_are_unique() -> None:
    for modelo in (Casa, Esporte, Competicao, Time, Mercado, Tipster):
        assert modelo.__table__.c.nome.unique is True
    assert Casa.__table__.c.dominio.unique is True
    assert Casa.__table__.c.dominio.nullable is True


def test_familia_is_a_native_enum_with_the_eight_families() -> None:
    assert FAMILIAS == (
        "GOLS",
        "CARTOES",
        "ESCANTEIOS",
        "RESULTADO",
        "HANDICAP",
        "JOGADOR",
        "AMBAS_MARCAM",
        "OUTRO",
    )
    assert isinstance(Mercado.__table__.c.familia.type, Enum)
    ddl = _ddl()
    assert (
        "CREATE TYPE familia_de_mercado AS ENUM ('GOLS', 'CARTOES', 'ESCANTEIOS', 'RESULTADO',"
        " 'HANDICAP', 'JOGADOR', 'AMBAS_MARCAM', 'OUTRO')"
    ) in ddl
    assert "familia familia_de_mercado DEFAULT 'OUTRO' NOT NULL" in _create_table("mercados")
    assert _indexes(Mercado) == {"idx_mercados_familia": ["familia"]}


def test_jsonb_columns() -> None:
    for coluna in (
        Time.__table__.c.apelidos,
        Tipster.__table__.c.apelidos,
        ExtracaoCache.__table__.c.resultado_json,
        ColetaCasa.__table__.c.bruto_json,
        RevisaoPendente.__table__.c.extracao_bruta,
        MovimentoRequisicao.__table__.c.resposta_json,
    ):
        assert isinstance(coluna.type, JSONB)
    assert "apelidos JSONB DEFAULT '[]'::jsonb NOT NULL" in _create_table("times")
    assert "apelidos JSONB DEFAULT '[]'::jsonb NOT NULL" in _create_table("tipsters")
    assert RevisaoPendente.__table__.c.extracao_bruta.nullable is True


def test_apelidos_are_unique_per_entity_type_and_typed() -> None:
    assert ENTIDADES == ("tipster", "casa", "time", "mercado", "esporte", "competicao")
    apelidos = _create_table("apelidos")
    assert "CONSTRAINT uq_apelidos_nome UNIQUE (entidade_tipo, nome)" in apelidos
    assert "CHECK (entidade_tipo IN ('tipster', 'casa', 'time', 'mercado'" in apelidos
    assert "confirmado BOOLEAN DEFAULT false NOT NULL" in apelidos
    assert _indexes(Apelido) == {"idx_apelidos_busca": ["entidade_tipo", "entidade_id"]}


def test_mensagens_are_unique_per_chat_and_message() -> None:
    mensagens = _create_table("mensagens")
    assert "CONSTRAINT uq_mensagens_chat_message UNIQUE (chat_id, message_id)" in mensagens
    assert "versao_atual INTEGER DEFAULT 1 NOT NULL" in mensagens
    assert "data TIMESTAMP WITH TIME ZONE NOT NULL" in mensagens
    assert _indexes(Mensagem) == {
        "idx_mensagens_data": ["data"],
        "idx_mensagens_autor": ["autor_bruto"],
        "idx_mensagens_midia_hash": ["midia_hash"],
    }
    assert Mensagem.__table__.c.autor_bruto.nullable is True
    assert Mensagem.__table__.c.editada_em.nullable is True
    assert Mensagem.__table__.c.midia_hash.nullable is True


def test_mensagem_versoes_are_partitioned_by_month() -> None:
    assert [c.name for c in MensagemVersao.__table__.primary_key.columns] == ["id", "criado_em"]
    versoes = _create_table("mensagem_versoes")
    assert "PARTITION BY RANGE (criado_em)" in versoes
    assert "id BIGSERIAL NOT NULL" in versoes
    assert _indexes(MensagemVersao) == {"idx_versoes_mensagem": ["mensagem_id"]}


def test_coletas_casa_keep_the_dedup_key_and_are_not_partitioned() -> None:
    coletas = _create_table("coletas_casa")
    assert (
        "CONSTRAINT uq_coletas_casa_identidade UNIQUE (usuario_id, casa_id, identidade)"
    ) in coletas
    assert "PARTITION BY" not in coletas
    assert _indexes(ColetaCasa) == {"idx_coletas_usuario": ["usuario_id", "casa_id"]}
    assert ColetaCasa.__table__.c.processado_em.nullable is True


def test_midias_are_deduplicated_by_hash() -> None:
    assert Midia.__table__.c.hash.unique is True
    assert "bytes BIGINT NOT NULL" in _create_table("midias")
    assert _indexes(Midia) == {"idx_midias_mensagem": ["mensagem_id"]}


def test_extracoes_cache_has_the_composite_key_of_appendix_b() -> None:
    chave = [c.name for c in ExtracaoCache.__table__.primary_key.columns]
    assert chave == ["chat_id", "message_id", "versao_prompt"]
    assert "id" not in ExtracaoCache.__table__.c
    assert "PRIMARY KEY (chat_id, message_id, versao_prompt)" in _create_table("extracoes_cache")


def test_chamadas_ia_cost_is_exact_decimal() -> None:
    tipo = ChamadaIA.__table__.c.custo_usd.type
    assert isinstance(tipo, Numeric)
    assert (tipo.precision, tipo.scale) == (12, 6)
    assert "custo_usd NUMERIC(12, 6) NOT NULL" in _create_table("chamadas_ia")
    assert _indexes(ChamadaIA) == {
        "idx_chamadas_usuario": ["usuario_id", "criado_em"],
        "idx_chamadas_data": ["criado_em"],
    }


def test_one_live_coleta_token_per_user() -> None:
    assert ColetaToken.__table__.c.token_hash.unique is True
    assert "ativo BOOLEAN DEFAULT true NOT NULL" in _create_table("coleta_token")
    assert (
        "CREATE UNIQUE INDEX idx_coleta_token_vivo ON coleta_token (usuario_id) WHERE ativo"
    ) in _ddl()
    assert ColetaToken.__table__.c.expira_em.nullable is True


def test_revisao_pendente_indexes() -> None:
    assert _indexes(RevisaoPendente) == {
        "idx_revisao_criado": ["criado_em"],
        "idx_revisao_usuario": ["usuario_id", "resolvido_em"],
    }
    assert RevisaoPendente.__table__.c.resolvido_em.nullable is True
    assert RevisaoPendente.__table__.c.midia_hash.nullable is True


def test_bigserial_except_composite_and_billing_owner_keys() -> None:
    for nome in set(Base.metadata.tables) - {"extracoes_cache", "assinaturas", "billing_rollout"}:
        assert "id BIGSERIAL NOT NULL" in _create_table(nome)
