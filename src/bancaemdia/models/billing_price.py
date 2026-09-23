from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class BillingPrice(Base):
    __tablename__ = "billing_prices"
    __table_args__ = (
        CheckConstraint("product = 'bancaemdia'", name="ck_billing_price_product"),
        CheckConstraint("amount_cents > 0", name="ck_billing_price_amount"),
        CheckConstraint("currency = 'BRL'", name="ck_billing_price_currency"),
        CheckConstraint("frequency IN ('MONTHLY', 'YEARLY')", name="ck_billing_price_frequency"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from", name="ck_billing_price_dates"
        ),
        CheckConstraint(
            "NOT published OR published_at IS NOT NULL", name="ck_billing_price_publication"
        ),
        ExcludeConstraint(
            ("product", "="),
            (text("tstzrange(valid_from, valid_until, '[)')"), "&&"),
            name="ex_billing_price_published_overlap",
            where=text("published"),
            using="gist",
        ),
        Index("idx_billing_price_product_from", "product", "valid_from"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product: Mapped[str] = mapped_column(String, server_default=text("'bancaemdia'"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    frequency: Mapped[str] = mapped_column(String)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_plan_ref: Mapped[str | None] = mapped_column(String)
    published: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
