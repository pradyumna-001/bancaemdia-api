from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Titular(Base):
    __tablename__ = "titulares"
    __table_args__ = (
        UniqueConstraint("usuario_id", "id", name="uq_titulares_usuario_id"),
        Index("idx_titulares_usuario_nome", "usuario_id", "nome"),
        CheckConstraint("length(btrim(nome)) BETWEEN 1 AND 160", name="ck_titulares_nome"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    nome: Mapped[str] = mapped_column(String(160))
    arquivado: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
