from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base

ESTADOS = ("PENDENTE", "GREEN", "RED", "ANULADA", "MEIO_GREEN", "MEIO_RED", "CASHOUT")
ORIGENS = ("telegram", "telegram_bot", "print", "manual", "planilha", "casa")


class Aposta(Base):
    __tablename__ = "apostas"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id",
            "chat_id",
            "message_id",
            "ordem_na_mensagem",
            name="uq_apostas_mensagem_ordem",
        ),
        CheckConstraint(
            f"estado IN ({', '.join(repr(e) for e in ESTADOS)})", name="ck_apostas_estado"
        ),
        CheckConstraint(
            f"origem IN ({', '.join(repr(o) for o in ORIGENS)})", name="ck_apostas_origem"
        ),
        CheckConstraint(
            "origem <> 'telegram' OR (chat_id IS NOT NULL AND message_id IS NOT NULL)",
            name="ck_apostas_telegram_tem_mensagem",
        ),
        CheckConstraint("stake_centavos >= 0", name="ck_apostas_stake"),
        CheckConstraint("NOT freebet OR stake_centavos = 0", name="ck_apostas_freebet"),
        Index("idx_apostas_usuario", "usuario_id", "criada_em"),
        Index("idx_apostas_estado", "usuario_id", "estado"),
        Index("idx_apostas_banca", "banca_id", "criada_em"),
        Index("idx_apostas_origem", "usuario_id", "origem"),
        Index("idx_apostas_mensagem", "chat_id", "message_id"),
        Index("idx_apostas_mercado", "mercado_id"),
        Index("idx_apostas_time", "time_casa_id"),
        Index("idx_apostas_competicao", "usuario_id", "competicao_id"),
        Index("idx_apostas_data", "usuario_id", "data_aposta"),
        Index("idx_apostas_duplicada", "usuario_id", "duplicada_de"),
        Index(
            "idx_apostas_print_ordem",
            "usuario_id",
            "midia_hash",
            "ordem_na_mensagem",
            postgresql_where=text("origem = 'print' AND midia_hash IS NOT NULL"),
        ),
        Index(
            "idx_apostas_revisao",
            "usuario_id",
            "revisao_grave",
            postgresql_where=text("revisao_grave"),
        ),
        Index(
            "idx_apostas_apagadas",
            "usuario_id",
            "criada_em",
            postgresql_where=text("NOT selecionada"),
        ),
        Index(
            "idx_apostas_chave",
            "usuario_id",
            "chave",
            unique=True,
            postgresql_where=text("chave IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    chave: Mapped[str | None] = mapped_column(String)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    ordem_na_mensagem: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    midia_hash: Mapped[str | None] = mapped_column(String)
    banca_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("bancas.id"))
    conta_casa_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("contas_casa.id"))
    tipster_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tipsters.id"))
    time_casa_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("times.id"))
    time_fora_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("times.id"))
    mercado_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("mercados.id"))
    competicao_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("competicoes.id"))
    data_aposta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_jogo: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stake_unidades: Mapped[float] = mapped_column(Float)
    stake_centavos: Mapped[int] = mapped_column(BigInteger)
    valor_aposta_centavos: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    odd: Mapped[float | None] = mapped_column(Float)
    retorno_centavos: Mapped[int | None] = mapped_column(BigInteger)
    estado: Mapped[str] = mapped_column(String, server_default=text("'PENDENTE'"))
    origem: Mapped[str] = mapped_column(String)
    freebet: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    duvida_de_par: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    parceira_chave: Mapped[str | None] = mapped_column(String)
    duplicada_de: Mapped[str | None] = mapped_column(String)
    revisao_grave: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Apagar no projeto antigo nunca foi apagar: a aposta sai das contas e das listas, e o
    # histórico dela fica inteiro, para dar para desfazer.
    selecionada: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    atualizada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
