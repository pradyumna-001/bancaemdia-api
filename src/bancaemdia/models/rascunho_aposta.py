"""Tenant-owned, non-financial Telegram bet draft and correction ledger."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base

ACTIVE_DRAFT_STATUSES = (
    "AWAITING_EXTRACTION",
    "AWAITING_INFORMATION",
    "AWAITING_CONFIRMATION",
)
DRAFT_STATUSES = (*ACTIVE_DRAFT_STATUSES, "CONFIRMED", "CANCELLED", "FAILED")
ACTIVE_SQL = "status IN ('AWAITING_EXTRACTION','AWAITING_INFORMATION','AWAITING_CONFIRMATION')"


class RascunhoAposta(Base):
    __tablename__ = "rascunhos_aposta"
    __table_args__ = (
        UniqueConstraint("usuario_id", "id", name="uq_rascunhos_aposta_tenant_id"),
        UniqueConstraint(
            "usuario_id",
            "telegram_chat_id",
            "telegram_message_id",
            name="uq_rascunhos_aposta_origem",
        ),
        Index(
            "uq_rascunhos_aposta_chat_ativo",
            "usuario_id",
            "telegram_chat_id",
            unique=True,
            postgresql_where=text(ACTIVE_SQL),
        ),
        CheckConstraint(
            "status IN ('AWAITING_EXTRACTION','AWAITING_INFORMATION',"
            "'AWAITING_CONFIRMATION','CONFIRMED','CANCELLED','FAILED')",
            name="ck_rascunhos_aposta_status",
        ),
        CheckConstraint("version > 0", name="ck_rascunhos_aposta_version"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    telegram_update_id: Mapped[int] = mapped_column(BigInteger)
    media_reference_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    media_hash: Mapped[str | None] = mapped_column(String(64))
    source_metadata_json: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    coupon_candidates_json: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    extraction_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    extraction_next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    extraction_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extraction_lease_token: Mapped[str | None] = mapped_column(String(32))
    extraction_error_code: Mapped[str | None] = mapped_column(String(32))
    extraction_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fields_json: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    field_meta_json: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    missing_fields_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(32), server_default=text("'AWAITING_EXTRACTION'"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RascunhoCorrecao(Base):
    __tablename__ = "rascunho_correcoes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["usuario_id", "rascunho_id"],
            ["rascunhos_aposta.usuario_id", "rascunhos_aposta.id"],
            name="fk_rascunho_correcoes_tenant_draft",
            ondelete="CASCADE",
        ),
        UniqueConstraint("rascunho_id", "version", name="uq_rascunho_correcoes_version"),
        UniqueConstraint("rascunho_id", "telegram_update_id", name="uq_rascunho_correcoes_update"),
        CheckConstraint("version > 1", name="ck_rascunho_correcoes_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger)
    rascunho_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer)
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger)
    changes_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
