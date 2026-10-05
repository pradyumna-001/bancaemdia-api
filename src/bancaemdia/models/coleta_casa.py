from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class ColetaCasa(Base):
    __tablename__ = "coletas_casa"
    __table_args__ = (
        UniqueConstraint("usuario_id", "casa_id", "identidade", name="uq_coletas_casa_identidade"),
        Index("idx_coletas_usuario", "usuario_id", "casa_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    casa_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("casas.id"))
    identidade: Mapped[str] = mapped_column(String)
    hash_conteudo: Mapped[str] = mapped_column(String)
    bruto_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recebido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
