from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Banca(Base):
    __tablename__ = "bancas"
    __table_args__ = (
        UniqueConstraint("usuario_id", "nome", name="uq_bancas_usuario_nome"),
        CheckConstraint(
            "saldo_inicial_centavos IS NULL OR saldo_inicial_centavos >= 0",
            name="ck_bancas_saldo_inicial",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    nome: Mapped[str] = mapped_column(String)
    saldo_inicial_centavos: Mapped[int | None] = mapped_column(BigInteger)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
