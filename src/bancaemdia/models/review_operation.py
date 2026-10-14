"""Owned, immutable receipt for a human review intention."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ReviewOperation(Base):
    __tablename__ = "review_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revisao_id", "usuario_id"],
            ["revisao_pendente.id", "revisao_pendente.usuario_id"],
            ondelete="CASCADE",
        ),
    )

    usuario_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    revisao_id: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
