"""Tenant-isolated diagnostics; unsafe capture bodies are never persisted."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ReaderQuarantine(Base):
    __tablename__ = "reader_quarantine"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id",
            "capture_sha256",
            "error_code",
            "reason",
            name="uq_reader_quarantine_capture",
        ),
        Index("idx_reader_quarantine_owner_date", "usuario_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    capture_sha256: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
