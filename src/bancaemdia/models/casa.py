from sqlalchemy import BigInteger, Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Casa(Base):
    __tablename__ = "casas"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nome: Mapped[str] = mapped_column(String, unique=True)
    dominio: Mapped[str | None] = mapped_column(String, unique=True)
    ativa: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
