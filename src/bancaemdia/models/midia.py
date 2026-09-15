from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Midia(Base):
    __tablename__ = "midias"
    __table_args__ = (Index("idx_midias_mensagem", "mensagem_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mensagem_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("mensagens.id"))
    hash: Mapped[str] = mapped_column(String, unique=True)
    tipo: Mapped[str] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(BigInteger)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
