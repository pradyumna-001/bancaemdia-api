from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Esporte(Base):
    __tablename__ = "esportes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nome: Mapped[str] = mapped_column(String, unique=True)
    icone: Mapped[str | None] = mapped_column(String)
