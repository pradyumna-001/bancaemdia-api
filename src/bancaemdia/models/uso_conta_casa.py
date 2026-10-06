from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class UsoContaCasa(Base):
    __tablename__ = "usos_conta_casa"
    __table_args__ = (
        ForeignKeyConstraint(
            ["usuario_id", "casa_id", "conta_casa_id"],
            ["contas_casa.usuario_id", "contas_casa.casa_id", "contas_casa.id"],
            name="fk_usos_conta_casa_identidade",
        ),
        CheckConstraint(
            "vigente_de IS NULL OR vigente_ate IS NULL OR vigente_de < vigente_ate",
            name="ck_usos_conta_casa_intervalo",
        ),
        CheckConstraint("origem IN ('LEGADO','EXPLICITA')", name="ck_usos_conta_casa_origem"),
        CheckConstraint(
            "origem = 'LEGADO' OR vigente_de IS NOT NULL",
            name="ck_usos_conta_casa_inicio_explicito",
        ),
        Index("idx_usos_conta_casa_tempo", "usuario_id", "casa_id", "vigente_de"),
        Index(
            "uq_usos_conta_casa_um_aberto_v1",
            "usuario_id",
            "casa_id",
            unique=True,
            postgresql_where=text("vigente_ate IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger)
    casa_id: Mapped[int] = mapped_column(BigInteger)
    conta_casa_id: Mapped[int] = mapped_column(BigInteger)
    vigente_de: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vigente_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origem: Mapped[str] = mapped_column(String, server_default=text("'EXPLICITA'"))
