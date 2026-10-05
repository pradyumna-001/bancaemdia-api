from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class MetaDesempenho(Base):
    __tablename__ = "metas_desempenho"
    __table_args__ = (
        CheckConstraint("inicio <= fim", name="ck_metas_desempenho_intervalo"),
        CheckConstraint(
            "metrica IN ('lucro_centavos', 'giro_centavos', 'roi', 'win_rate', 'total_apostas')",
            name="ck_metas_desempenho_metrica",
        ),
        CheckConstraint(
            "status IN ('ativa', 'concluida', 'arquivada')",
            name="ck_metas_desempenho_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"), nullable=False)
    titulo: Mapped[str] = mapped_column(String(120), nullable=False)
    metrica: Mapped[str] = mapped_column(String(32), nullable=False)
    inicio: Mapped[date] = mapped_column(Date, nullable=False)
    fim: Mapped[date] = mapped_column(Date, nullable=False)
    alvo: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    linha_base: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ativa")
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    atualizada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
