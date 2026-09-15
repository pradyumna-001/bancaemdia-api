from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Usuario(Base):
    __tablename__ = "usuarios"
    __table_args__ = (Index("usuarios_email_unico", text("lower(email)"), unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String)
    nome: Mapped[str] = mapped_column(String)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ativo: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
