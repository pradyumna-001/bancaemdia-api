from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ManualCreationOperation(Base):
    __tablename__ = "manual_creation_operations"

    usuario_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("usuarios.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    receipt: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
