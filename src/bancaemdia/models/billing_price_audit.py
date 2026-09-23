"""Append-only audit of catalog changes without price values or provider references."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class BillingPriceAudit(Base):
    __tablename__ = "billing_price_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    price_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String)
    fields: Mapped[list[str]] = mapped_column(JSONB)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
