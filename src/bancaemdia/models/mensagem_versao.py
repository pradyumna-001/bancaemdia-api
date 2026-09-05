from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class MensagemVersao(Base):
    __tablename__ = "mensagem_versoes"
    __table_args__ = (
        Index("idx_versoes_mensagem", "mensagem_id"),
        {"postgresql_partition_by": "RANGE (criado_em)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mensagem_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mensagens.id"))
    texto: Mapped[str] = mapped_column(String)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
