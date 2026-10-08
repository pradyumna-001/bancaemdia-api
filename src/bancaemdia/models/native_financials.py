"""Denominated ledger kept apart from the legacy centavo projections."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class NativeAccount(Base):
    __tablename__ = "native_accounts"
    __table_args__ = (
        UniqueConstraint("id", "usuario_id", "currency", name="uq_native_account_currency_owner"),
        CheckConstraint("currency IN ('BRL','USDT')", name="ck_native_account_currency"),
        CheckConstraint(
            "valid_to IS NULL OR valid_from < valid_to", name="ck_native_account_period"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    casa_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("casas.id"))
    currency: Mapped[str] = mapped_column(String(4))
    label: Mapped[str] = mapped_column(String(120))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class NativeBet(Base):
    __tablename__ = "native_bets"
    __table_args__ = (
        UniqueConstraint("usuario_id", "identity_hash", name="uq_native_bet_owner_identity"),
        UniqueConstraint("id", "usuario_id", name="uq_native_bet_owner"),
        ForeignKeyConstraint(
            ["account_id", "usuario_id", "currency"],
            ["native_accounts.id", "native_accounts.usuario_id", "native_accounts.currency"],
            name="fk_native_bet_account_currency_owner",
        ),
        CheckConstraint("currency IN ('BRL','USDT')", name="ck_native_bet_currency"),
        CheckConstraint("stake > 0 AND returned >= 0", name="ck_native_bet_amounts"),
        CheckConstraint("state IN ('GREEN','RED')", name="ck_native_bet_state"),
        CheckConstraint("state <> 'RED' OR returned = 0", name="ck_native_bet_loss"),
        CheckConstraint("state <> 'GREEN' OR returned > 0", name="ck_native_bet_win"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(4))
    identity_hash: Mapped[str] = mapped_column(String(64))
    canonical_hash: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    stake: Mapped[Decimal] = mapped_column(Numeric(50, 30))
    returned: Mapped[Decimal] = mapped_column(Numeric(50, 30))
    game_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    canonical: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NativeBetEvidence(Base):
    __tablename__ = "native_bet_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["bet_id", "usuario_id"],
            ["native_bets.id", "native_bets.usuario_id"],
            name="fk_native_evidence_bet_owner",
        ),
        UniqueConstraint("usuario_id", "bet_id", "source_hash", name="uq_native_evidence_content"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    bet_id: Mapped[int] = mapped_column(BigInteger)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    canonical: Mapped[dict[str, object]] = mapped_column(JSONB)
    envelope: Mapped[dict[str, object]] = mapped_column(JSONB)
