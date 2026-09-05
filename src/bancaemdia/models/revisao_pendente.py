from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class RevisaoPendente(Base):
    __tablename__ = "revisao_pendente"
    __table_args__ = (
        Index("idx_revisao_criado", "criado_em"),
        Index("idx_revisao_usuario", "usuario_id", "resolvido_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    midia_hash: Mapped[str | None] = mapped_column(String)
    motivo: Mapped[str] = mapped_column(String)
    extracao_bruta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolvido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
