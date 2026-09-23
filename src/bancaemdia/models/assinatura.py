from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Assinatura(Base):
    __tablename__ = "assinaturas"
    __table_args__ = (
        CheckConstraint(
            "status IN ('TRIALING', 'ACTIVE', 'PAST_DUE', 'CANCELED', 'EXPIRED')",
            name="ck_assinatura_status",
        ),
        CheckConstraint(
            "trial_ends_at = trial_started_at + interval '7 days'",
            name="ck_assinatura_trial_length",
        ),
        CheckConstraint(
            "(current_period_started_at IS NULL AND current_period_ends_at IS NULL) "
            "OR (current_period_started_at IS NOT NULL AND current_period_ends_at > current_period_started_at)",
            name="ck_assinatura_period",
        ),
        CheckConstraint(
            "status <> 'ACTIVE' OR (price_id IS NOT NULL AND current_period_started_at IS NOT NULL)",
            name="ck_assinatura_active_terms",
        ),
        Index("idx_assinaturas_status", "status"),
    )

    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String)
    trial_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_period_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("billing_prices.id"))
    provider: Mapped[str | None] = mapped_column(String)
    provider_customer_ref: Mapped[str | None] = mapped_column(String)
    provider_subscription_ref: Mapped[str | None] = mapped_column(String)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
