"""One durable checkout operation per account, surviving provider timeout and process crash."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class BillingCheckout(Base):
    __tablename__ = "billing_checkouts"
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"), primary_key=True)
    operation: Mapped[str] = mapped_column(String, unique=True)
    request_hash: Mapped[str] = mapped_column(String)
    price_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("billing_prices.id"))
    trial: Mapped[bool] = mapped_column(Boolean)
    session_ref: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
