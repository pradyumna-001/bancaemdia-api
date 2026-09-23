"""Durable Telegram transport inbox and tenant-owned transactional outbox."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class TelegramInbox(Base):
    __tablename__ = "telegram_inbox"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','DONE','DLQ')", name="ck_telegram_inbox_status"),
        CheckConstraint("attempts BETWEEN 0 AND 8", name="ck_telegram_inbox_attempts"),
        Index(
            "idx_telegram_inbox_due",
            "next_attempt_at",
            "id",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    event_type: Mapped[str] = mapped_column(String(24))
    sender_user_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    payload_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramOutbox(Base):
    __tablename__ = "telegram_outbox"
    __table_args__ = (
        UniqueConstraint("usuario_id", "idempotency_key", name="uq_telegram_outbox_key"),
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','DLQ')", name="ck_telegram_outbox_status"
        ),
        CheckConstraint("attempts BETWEEN 0 AND 8", name="ck_telegram_outbox_attempts"),
        Index(
            "idx_telegram_outbox_due",
            "next_attempt_at",
            "id",
            postgresql_where=text("status IN ('PENDING','SENDING')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    payload_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
