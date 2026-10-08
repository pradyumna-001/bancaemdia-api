"""Private groups with explicit, stable bet membership, never inferred from names."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class GrupoAposta(Base):
    __tablename__ = "grupos_aposta"
    __table_args__ = (
        UniqueConstraint("usuario_id", "id", name="uq_grupos_aposta_usuario_id"),
        UniqueConstraint("usuario_id", "nome", name="uq_grupos_aposta_nome"),
        CheckConstraint("length(btrim(nome)) BETWEEN 1 AND 160", name="ck_grupos_aposta_nome"),
        Index("idx_grupos_aposta_usuario", "usuario_id", "arquivado"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    nome: Mapped[str] = mapped_column(String(160))
    arquivado: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApostaGrupo(Base):
    __tablename__ = "apostas_grupos"
    __table_args__ = (
        ForeignKeyConstraint(
            ["usuario_id", "grupo_id"],
            ["grupos_aposta.usuario_id", "grupos_aposta.id"],
            name="fk_apostas_grupos_grupo_usuario",
        ),
        ForeignKeyConstraint(
            ["aposta_id", "usuario_id"],
            ["apostas.id", "apostas.usuario_id"],
            name="fk_apostas_grupos_aposta_usuario",
        ),
        Index("idx_apostas_grupos_selecao", "usuario_id", "grupo_id", "aposta_id"),
    )

    usuario_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    aposta_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    grupo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vinculado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
