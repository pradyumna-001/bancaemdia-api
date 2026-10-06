from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base

TIPOS_DE_MOVIMENTO = ("DEPOSITO", "SAQUE", "TRANSFERENCIA", "BONUS", "AJUSTE")


class Movimento(Base):
    __tablename__ = "movimentos"
    __table_args__ = (
        CheckConstraint(
            f"tipo IN ({', '.join(repr(t) for t in TIPOS_DE_MOVIMENTO)})",
            name="ck_movimentos_tipo",
        ),
        CheckConstraint("valor_centavos <> 0", name="ck_movimentos_valor"),
        Index("idx_movimentos_usuario", "usuario_id", "ocorrido_em"),
        Index("idx_movimentos_conta", "conta_casa_id", "ocorrido_em"),
        # Uma transferência são duas linhas do mesmo lançamento. O UUID permite reuni-las sem
        # tornar obrigatório um vínculo para depósito, saque, bônus ou ajuste.
        Index(
            "idx_movimentos_transferencia",
            "usuario_id",
            "transferencia_id",
            postgresql_where=text("transferencia_id IS NOT NULL"),
        ),
        {"postgresql_partition_by": "RANGE (ocorrido_em)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    conta_casa_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("contas_casa.id"))
    tipo: Mapped[str] = mapped_column(String)
    valor_centavos: Mapped[int] = mapped_column(BigInteger)
    ocorrido_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    descricao: Mapped[str | None] = mapped_column(String)
    transferencia_id: Mapped[UUID | None] = mapped_column(postgresql.UUID(as_uuid=True))
