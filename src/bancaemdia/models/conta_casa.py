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
        ForeignKeyConstraint(
            ["usuario_id", "titular_id"],
            ["titulares.usuario_id", "titulares.id"],
            name="fk_contas_casa_titular_usuario",
        ),
        UniqueConstraint("usuario_id", "id", name="uq_contas_casa_usuario_id"),
        UniqueConstraint("usuario_id", "casa_id", "id", name="uq_contas_casa_usuario_casa_id"),
        Index("idx_contas_casa_titular", "usuario_id", "titular_id"),
        Index(
            "uq_contas_casa_titular_casa",
            "usuario_id",
            "casa_id",
            "titular_id",
            unique=True,
            postgresql_where=text("titular_id IS NOT NULL"),
        ),
        CheckConstraint(
            "estado IN ('DISPONIVEL','EM_USO','LIMITADA','ENCERRADA')",
            name="ck_contas_casa_estado",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    casa_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("casas.id"))
    banca_id: Mapped[int | None] = mapped_column(BigInteger)
    titular_id: Mapped[int | None] = mapped_column(BigInteger)
    estado: Mapped[str] = mapped_column(String, server_default=text("'DISPONIVEL'"))
    apelido: Mapped[str] = mapped_column(String, server_default=text("''"))
    desde: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ativa: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
