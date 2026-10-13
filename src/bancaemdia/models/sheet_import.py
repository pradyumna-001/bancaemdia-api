"""Private, owner-scoped spreadsheet drafts and durable application identities."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class SheetSource(Base):
    __tablename__ = "sheet_sources"
    usuario_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("usuarios.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    nome: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=0)
    current_preview: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SheetPreview(Base):
    __tablename__ = "sheet_previews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["usuario_id", "source_id"],
            ["sheet_sources.usuario_id", "sheet_sources.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("usuario_id", "id", "source_id"),
    )
    usuario_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    revision: Mapped[int] = mapped_column(Integer)
    source_version: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    preview_hash: Mapped[str] = mapped_column(String(64))
    file_hash: Mapped[str] = mapped_column(String(64))
    private_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SheetConfirmation(Base):
    __tablename__ = "sheet_confirmations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["usuario_id", "preview_id", "source_id"],
            ["sheet_previews.usuario_id", "sheet_previews.id", "sheet_previews.source_id"],
            ondelete="CASCADE",
        ),
    )
    usuario_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    preview_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    request_hash: Mapped[str] = mapped_column(String(64))
    private_result: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
