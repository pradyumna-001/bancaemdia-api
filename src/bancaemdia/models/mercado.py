from sqlalchemy import BigInteger, Enum, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base

FAMILIAS = (
    "GOLS",
    "CARTOES",
    "ESCANTEIOS",
    "RESULTADO",
    "HANDICAP",
    "JOGADOR",
    "AMBAS_MARCAM",
    "OUTRO",
)


class Mercado(Base):
    __tablename__ = "mercados"
    __table_args__ = (Index("idx_mercados_familia", "familia"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nome: Mapped[str] = mapped_column(String, unique=True)
    familia: Mapped[str] = mapped_column(
        Enum(*FAMILIAS, name="familia_de_mercado"), server_default=text("'OUTRO'")
    )
