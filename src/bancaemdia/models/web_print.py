"""Tenant-owned web intake, durable dedupe tombstones, and private original bytes."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class PrintIntent(Base):
    __tablename__ = "print_intents"
    usuario_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("usuarios.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PrintAsset(Base):
    __tablename__ = "print_assets"
    __table_args__ = (
        UniqueConstraint("usuario_id", "digest"),
        UniqueConstraint("usuario_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    usuario_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("usuarios.id", ondelete="CASCADE")
    )
    digest: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(Integer)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    media_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(24), default="accepted")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    quota_day: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retry_token: Mapped[UUID | None]
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(40))
    retryable: Mapped[bool] = mapped_column(default=False)
    reading: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)


class PrintItem(Base):
    __tablename__ = "print_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["usuario_id", "intent_id"],
            ["print_intents.usuario_id", "print_intents.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["usuario_id", "asset_id"], ["print_assets.usuario_id", "print_assets.id"]
        ),
    )
    usuario_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    intent_id: Mapped[UUID] = mapped_column(primary_key=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    asset_id: Mapped[UUID] = mapped_column()
    duplicate: Mapped[bool] = mapped_column(default=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
