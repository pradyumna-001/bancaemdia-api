from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class CruzamentoEntrada(Base):
    """Bounded, rebuildable matching projection; never a financial projection."""

    __tablename__ = "cruzamento_entradas"
    __table_args__ = (
        ForeignKeyConstraint(
            ["aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"], ondelete="CASCADE"
        ),
        Index("ix_cruzamento_busca", "usuario_id", "casa", "origem", "ocorrido_em", "aposta_id"),
    )
    aposta_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    casa: Mapped[str | None] = mapped_column(String)
    origem: Mapped[str] = mapped_column(String)
    ocorrido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dados: Mapped[dict[str, Any]] = mapped_column(JSONB)
    atualizada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CruzamentoCandidato(Base):
    __tablename__ = "cruzamento_candidatos"
    __table_args__ = (
        ForeignKeyConstraint(
            ["casa_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        ForeignKeyConstraint(
            ["telegram_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        UniqueConstraint(
            "casa_aposta_id", "telegram_aposta_id", "versao", name="uq_cruzamento_par_versao"
        ),
        CheckConstraint("score BETWEEN 0 AND 100", name="ck_cruzamento_score"),
        CheckConstraint(
            "status IN ('exact','probable','incompatible','excluded')", name="ck_cruzamento_status"
        ),
        Index("ix_cruzamento_casa", "usuario_id", "casa_aposta_id", "status"),
        Index("ix_cruzamento_telegram", "usuario_id", "telegram_aposta_id", "status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    casa_aposta_id: Mapped[int] = mapped_column(BigInteger)
    telegram_aposta_id: Mapped[int] = mapped_column(BigInteger)
    versao: Mapped[str] = mapped_column(String(40))
    score: Mapped[int] = mapped_column(Integer)
    classe_base: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    busca_truncada: Mapped[bool] = mapped_column(Boolean, default=False)
    sinais_iguais: Mapped[list[str]] = mapped_column(JSONB)
    sinais_conflitantes: Mapped[list[str]] = mapped_column(JSONB)
    evidencia: Mapped[dict[str, Any]] = mapped_column(JSONB)
    explicacao: Mapped[str] = mapped_column(String)
    revisao_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("revisao_pendente.id"))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
