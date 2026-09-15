from sqlalchemy import BigInteger, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Tipster(Base):
    __tablename__ = "tipsters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nome: Mapped[str] = mapped_column(String, unique=True)
    apelidos: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
