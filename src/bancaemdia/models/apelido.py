from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base

ENTIDADES = ("tipster", "casa", "time", "mercado", "esporte", "competicao")


class Apelido(Base):
    __tablename__ = "apelidos"
    __table_args__ = (
        UniqueConstraint("entidade_tipo", "nome", name="uq_apelidos_nome"),
        CheckConstraint(
            f"entidade_tipo IN ({', '.join(repr(e) for e in ENTIDADES)})",
            name="ck_apelidos_entidade_tipo",
        ),
        Index("idx_apelidos_busca", "entidade_tipo", "entidade_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entidade_tipo: Mapped[str] = mapped_column(String)
    entidade_id: Mapped[int] = mapped_column(BigInteger)
    nome: Mapped[str] = mapped_column(String)
    confirmado: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
