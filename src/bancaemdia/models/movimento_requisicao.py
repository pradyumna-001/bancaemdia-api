from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bancaemdia.db.models import Base


class MovimentoRequisicao(Base):
    """Resposta imutável de uma escrita de caixa identificada pelo cliente."""

    __tablename__ = "movimento_requisicoes"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id",
            "chave_idempotencia",
            name="uq_movimento_requisicoes_usuario_chave",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("usuarios.id"))
    chave_idempotencia: Mapped[str] = mapped_column(String(200))
    requisicao_hash: Mapped[str] = mapped_column(String(64))
    resposta_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
