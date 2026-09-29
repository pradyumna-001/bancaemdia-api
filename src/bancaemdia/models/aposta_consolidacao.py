"""Durable decisions; source rows and financial values remain intact."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ApostaConsolidacao(Base):
    __tablename__ = "aposta_consolidacoes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["casa_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        ForeignKeyConstraint(
            ["telegram_aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"]
        ),
        ForeignKeyConstraint(
            ["conta_casa_id", "usuario_id"], ["contas_casa.id", "contas_casa.usuario_id"]
        ),
        CheckConstraint("casa_aposta_id <> telegram_aposta_id", name="ck_consolidacao_pontas"),
        CheckConstraint(
            "estado IN ('active','unlinked','rejected')", name="ck_consolidacao_estado"
        ),
        CheckConstraint(
            "decisao IN ('automatic','reviewed','legacy')", name="ck_consolidacao_decisao"
        ),
        CheckConstraint(
            "(estado = 'active' AND desvinculada_em IS NULL) OR "
            "(estado IN ('unlinked','rejected') AND desvinculada_em IS NOT NULL)",
            name="ck_consolidacao_desvinculacao",
        ),
        Index(
            "uq_consolidacao_casa_ativa",
            "casa_aposta_id",
            unique=True,
            postgresql_where=text("estado = 'active'"),
        ),
        Index(
            "uq_consolidacao_telegram_ativa",
            "telegram_aposta_id",
            unique=True,
            postgresql_where=text("estado = 'active'"),
        ),
        Index("ix_consolidacao_historico", "usuario_id", "casa_aposta_id", "telegram_aposta_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger)
    casa_aposta_id: Mapped[int] = mapped_column(BigInteger)
    telegram_aposta_id: Mapped[int] = mapped_column(BigInteger)
    conta_casa_id: Mapped[int | None] = mapped_column(BigInteger)
    estado: Mapped[str] = mapped_column(String(16), default="active")
    decisao: Mapped[str] = mapped_column(String(16))
    versao: Mapped[str] = mapped_column(String(40))
    evidencia: Mapped[dict[str, Any]] = mapped_column(JSONB)
    contexto: Mapped[dict[str, Any]] = mapped_column(JSONB)
    ator: Mapped[str] = mapped_column(String(40))
    criada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    desvinculada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    motivo_desvinculacao: Mapped[str | None] = mapped_column(String)
