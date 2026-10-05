from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class TrocaTitularRequisicao(Base):
    __tablename__ = "trocas_titular_requisicoes"
    __table_args__ = (
        UniqueConstraint("usuario_id", "tipo", "chave", name="uq_trocas_titular_idempotencia"),
        Index("idx_trocas_titular_requisicoes_usuario", "usuario_id", "criado_em"),
        CheckConstraint("tipo IN ('PREVIEW','APPLY')", name="ck_trocas_titular_tipo"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    tipo: Mapped[str] = mapped_column(String(16))
    chave: Mapped[str] = mapped_column(String(160))
    pedido_hash: Mapped[str] = mapped_column(String(64))
    resposta: Mapped[dict[str, object]] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrocaTitularEvento(Base):
    __tablename__ = "trocas_titular_eventos"
    __table_args__ = (Index("idx_trocas_titular_eventos_usuario", "usuario_id", "criado_em"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    tipo: Mapped[str] = mapped_column(String(16))
    requisicao_id: Mapped[int] = mapped_column(BigInteger)
    dados: Mapped[dict[str, object]] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
