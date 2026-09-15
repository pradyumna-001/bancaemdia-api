from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ColetaToken(Base):
    __tablename__ = "coleta_token"
    __table_args__ = (
        Index("idx_coleta_token_vivo", "usuario_id", unique=True, postgresql_where=text("ativo")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    ativo: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expira_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
