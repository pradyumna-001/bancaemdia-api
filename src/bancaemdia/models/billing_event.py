"""Minimal durable inbox. No raw webhook, card data, emails, or hosted URLs."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class BillingEvent(Base):
    __tablename__ = "billing_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_ref: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    state: Mapped[str] = mapped_column(String, server_default=text("'pending'"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String)
