from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ContaCasa(Base):
    __tablename__ = "contas_casa"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id", "casa_id", "apelido", name="uq_contas_casa_usuario_casa_apelido"
        ),
        Index("idx_contas_casa_usuario", "usuario_id", "ativa"),
        Index("idx_contas_casa_banca", "usuario_id", "banca_id"),
        ForeignKeyConstraint(
            ["usuario_id", "banca_id"],
            ["bancas.usuario_id", "bancas.id"],
            name="fk_contas_casa_banca_usuario",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    casa_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("casas.id"))
    banca_id: Mapped[int | None] = mapped_column(BigInteger)
    apelido: Mapped[str] = mapped_column(String, server_default=text("''"))
    desde: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ativa: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
