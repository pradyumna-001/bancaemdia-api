"""Tenant-owned Telegram identity and short-lived account-link codes."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    __table_args__ = (
        UniqueConstraint("usuario_id", "telegram_user_id", name="uq_telegram_link_pair"),
        Index(
            "uq_telegram_link_active_user",
            "usuario_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "uq_telegram_link_active_identity",
            "telegram_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramLinkCode(Base):
    __tablename__ = "telegram_link_codes"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_telegram_link_code_hash"),
        Index("idx_telegram_link_codes_user_issued", "usuario_id", "issued_at"),
        CheckConstraint("failed_attempts BETWEEN 0 AND 5", name="ck_telegram_link_code_attempts"),
        CheckConstraint("expires_at > issued_at", name="ck_telegram_link_code_expiry"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    code_hash: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class TelegramLinkAttempt(Base):
    """Global, keyed sender throttle; it contains no raw Telegram identifier."""

    __tablename__ = "telegram_link_attempts"

    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_attempts: Mapped[int] = mapped_column(Integer)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TelegramLinkAttemptEvent(Base):
    """Anonymous, append-only audit for attempts with no known tenant."""

    __tablename__ = "telegram_link_attempt_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    outcome: Mapped[str] = mapped_column(
        String(16),
        CheckConstraint(
            "outcome IN ('INVALID','BLOCKED')", name="ck_telegram_link_attempt_outcome"
        ),
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
