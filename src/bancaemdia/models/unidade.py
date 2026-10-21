from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class Unidade(Base):
    __tablename__ = "unidades"
    __table_args__ = (
        CheckConstraint("valor_centavos > 0", name="ck_unidades_valor"),
        CheckConstraint(
            "(chave_idempotencia IS NULL AND requisicao_hash IS NULL AND recibo_json IS NULL)"
            " OR (chave_idempotencia IS NOT NULL AND requisicao_hash IS NOT NULL"
            " AND recibo_json IS NOT NULL)",
            name="ck_unidades_idempotencia",
        ),
        Index("idx_unidades_usuario", "usuario_id", "vigente_de"),
        Index(
            "uq_unidades_usuario_chave",
            "usuario_id",
            "chave_idempotencia",
            unique=True,
            postgresql_where=text("chave_idempotencia IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    valor_centavos: Mapped[int] = mapped_column(BigInteger)
    vigente_de: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    vigente_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Só a vigência criada pelo site: a identidade do cliente e o recibo devolvido no replay.
    chave_idempotencia: Mapped[str | None] = mapped_column(String(200))
    requisicao_hash: Mapped[str | None] = mapped_column(String(64))
    recibo_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
