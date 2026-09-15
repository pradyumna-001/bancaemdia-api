from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base

TIPOS_DE_EVENTO = (
    "APOSTA_CRIADA",
    "ODD_ALTERADA",
    "STAKE_ALTERADA",
    "RESULTADO_REGISTRADO",
    "CASHOUT_REGISTRADO",
    "APOSTA_ANULADA",
    "APOSTA_CANCELADA",
    "SELECAO_ALTERADA",
    "CORRECAO_MANUAL",
    "MOVIMENTO_REGISTRADO",
    "CLV_REGISTRADO",
)
FONTES = ("export", "ia", "manual", "liquidacao", "planilha", "casa")


class Evento(Base):
    __tablename__ = "eventos"
    __table_args__ = (
        CheckConstraint(
            f"tipo IN ({', '.join(repr(t) for t in TIPOS_DE_EVENTO)})", name="ck_eventos_tipo"
        ),
        CheckConstraint(
            f"fonte IN ({', '.join(repr(f) for f in FONTES)})", name="ck_eventos_fonte"
        ),
        Index("idx_eventos_usuario", "usuario_id", "criado_em"),
        Index("idx_eventos_chave", "usuario_id", "aposta_chave", "id"),
        {"postgresql_partition_by": "RANGE (criado_em)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    tipo: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fonte: Mapped[str] = mapped_column(String)
    confianca: Mapped[float | None] = mapped_column(Float)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    aposta_chave: Mapped[str | None] = mapped_column(String)
