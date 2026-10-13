"""Private durable intentions, authoritative preimages and immutable receipts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ResultOperation(Base):
    __tablename__ = "result_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["aposta_id", "usuario_id"], ["apostas.id", "apostas.usuario_id"], ondelete="CASCADE"
        ),
    )

    usuario_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    aposta_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(8))
    fingerprint: Mapped[str] = mapped_column(String(64))
    before: Mapped[dict[str, Any]] = mapped_column(JSONB)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
