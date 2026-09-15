from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Competicao(Base):
    __tablename__ = "competicoes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    esporte_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("esportes.id"))
    nome: Mapped[str] = mapped_column(String, unique=True)
    pais: Mapped[str | None] = mapped_column(String)
