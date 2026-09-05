from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ChamadaIA(Base):
    __tablename__ = "chamadas_ia"
    __table_args__ = (
        Index("idx_chamadas_usuario", "usuario_id", "criado_em"),
        Index("idx_chamadas_data", "criado_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    modelo: Mapped[str] = mapped_column(String)
    tokens_entrada: Mapped[int] = mapped_column(Integer)
    tokens_saida: Mapped[int] = mapped_column(Integer)
    custo_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
