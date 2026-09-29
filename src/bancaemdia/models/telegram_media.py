"""Private bot images, independent of globally deduplicated legacy exports."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class TelegramMedia(Base):
    __tablename__ = "telegram_media"

    draft_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("rascunhos_aposta.id", ondelete="CASCADE"),
        primary_key=True,
    )
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    content_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    content_hash: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
