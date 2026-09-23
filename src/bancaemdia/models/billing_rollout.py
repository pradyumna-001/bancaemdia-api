from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class BillingRollout(Base):
    __tablename__ = "billing_rollout"
    __table_args__ = (CheckConstraint("id = 1", name="ck_billing_rollout_singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
