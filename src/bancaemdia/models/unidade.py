from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Unidade(Base):
    __tablename__ = "unidades"
    __table_args__ = (
        CheckConstraint("valor_centavos > 0", name="ck_unidades_valor"),
        Index("idx_unidades_usuario", "usuario_id", "vigente_de"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    valor_centavos: Mapped[int] = mapped_column(BigInteger)
    vigente_de: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    vigente_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
